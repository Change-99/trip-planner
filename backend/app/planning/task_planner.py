"""第一层规划：决定还需要执行哪些真实工具任务。"""

from __future__ import annotations

import math

from app.models.planning import (
    EvidenceBundle,
    EvidenceReport,
    PlanningConstraints,
    TaskPlan,
    TaskSpec,
    TaskType,
    PriceCategory,
)


class TaskPlanner:
    """生成受白名单限制的结构化工具任务。

    Python 保证必需的数据任务不会被遗漏；后续可在此基础上接入 LLM，
    让 LLM 只负责补充搜索关键词，而不能创造未知工具。
    """

    def __init__(
        self,
        *,
        max_candidates: int = 8,
        nearest_neighbors: int = 1,
        max_price_candidates: int = 4,
    ) -> None:
        self.max_candidates = max_candidates
        self.nearest_neighbors = nearest_neighbors
        self.max_price_candidates = max_price_candidates

    def plan(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        report: EvidenceReport | None,
        round_index: int,
    ) -> TaskPlan:
        tasks: list[TaskSpec] = []

        usable_ids = set(report.usable_poi_ids if report else [])
        target_count = min(max(constraints.travel_days, 2), self.max_candidates)
        if len(usable_ids) < target_count:
            tasks.extend(self._poi_search_tasks(constraints, round_index))

        if round_index == 0:
            tasks.append(
                TaskSpec(
                    task_id="weather_0",
                    task_type=TaskType.QUERY_WEATHER,
                    arguments={"city": constraints.city},
                    required_fields=["forecasts"],
                    critical=False,
                    reason="天气不是排程硬约束，但可用于风险提示",
                )
            )
            tasks.append(
                TaskSpec(
                    task_id="hotel_0",
                    task_type=TaskType.SEARCH_HOTEL,
                    arguments={
                        "city": constraints.city,
                        "keywords": constraints.accommodation or "酒店",
                        "limit": 5,
                    },
                    required_fields=["poi_id", "location"],
                    critical=False,
                    reason="查询住宿候选，不允许模型编造酒店",
                )
            )

        if report and len(usable_ids) >= target_count:
            tasks.extend(
                self._route_tasks(
                    constraints,
                    evidence,
                    usable_ids,
                    round_index,
                )
            )
            tasks.extend(
                self._price_tasks(
                    constraints,
                    evidence,
                    usable_ids,
                    round_index,
                )
            )

        return TaskPlan(
            round_index=round_index,
            tasks=self._deduplicate(tasks),
            rationale="先补齐候选景点证据，再查询候选景点之间的真实交通耗时",
        )

    def _poi_search_tasks(
        self,
        constraints: PlanningConstraints,
        round_index: int,
    ) -> list[TaskSpec]:
        keywords: list[str] = []
        keywords.extend(constraints.must_visit)
        keywords.extend(constraints.preferences or ["景点"])
        if round_index > 0:
            keywords.extend(["博物馆", "公园", "城市地标"])

        tasks: list[TaskSpec] = []
        seen: set[str] = set()
        for index, keyword in enumerate(keywords):
            normalized = keyword.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            tasks.append(
                TaskSpec(
                    task_id=f"poi_{round_index}_{index}",
                    task_type=TaskType.SEARCH_POI,
                    arguments={
                        "city": constraints.city,
                        "keywords": normalized,
                        "limit": self.max_candidates,
                    },
                    required_fields=[
                        "poi_id",
                        "location",
                        "opening_hours",
                    ],
                    critical=True,
                    reason=f"为偏好或必去条件“{normalized}”收集真实候选景点",
                )
            )
        return tasks

    def _price_tasks(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        usable_ids: set[str],
        round_index: int,
    ) -> list[TaskSpec]:
        """为预算收集有来源的价格；失败不会阻止时间约束规划。"""

        existing = {
            (quote.category, quote.entity_id)
            for quote in evidence.price_quotes
        }
        tasks: list[TaskSpec] = []
        candidates = [poi for poi in evidence.pois if poi.poi_id in usable_ids]
        required = [
            poi
            for poi in candidates
            if any(
                must_visit in poi.name or poi.name in must_visit
                for must_visit in constraints.must_visit
            )
        ]
        required_ids = {poi.poi_id for poi in required}
        optional = sorted(
            (poi for poi in candidates if poi.poi_id not in required_ids),
            key=lambda poi: poi.rating or 0,
            reverse=True,
        )
        # 价格是软约束，先查必去点和高评分候选。未查的项目
        # 在最终预算中保持 unknown，不会被当作 0 元。
        price_limit = max(self.max_price_candidates, len(required))
        candidates = [*required, *optional[: max(0, price_limit - len(required))]]
        for poi in candidates:
            key = (PriceCategory.TICKET, poi.poi_id)
            if key in existing:
                continue
            tasks.append(
                TaskSpec(
                    task_id=f"ticket_price_{round_index}_{poi.poi_id}",
                    task_type=TaskType.QUERY_TICKET_PRICE,
                    arguments={
                        "quote_id": f"ticket:{poi.poi_id}",
                        "entity_id": poi.poi_id,
                        "entity_name": poi.name,
                        "city": constraints.city,
                        "visit_start": constraints.start_date.isoformat(),
                        "visit_end": constraints.end_date.isoformat(),
                    },
                    required_fields=["status", "source_url"],
                    critical=False,
                    reason="门票价格只能来自可追溯的实时外部来源",
                )
            )

        if evidence.hotels and constraints.travel_days > 1:
            hotel = max(evidence.hotels, key=lambda item: item.rating or 0)
            key = (PriceCategory.HOTEL_NIGHT, hotel.poi_id)
            if key not in existing:
                tasks.append(
                    TaskSpec(
                        task_id=f"hotel_price_{round_index}_{hotel.poi_id}",
                        task_type=TaskType.QUERY_HOTEL_PRICE,
                        arguments={
                            "quote_id": f"hotel:{hotel.poi_id}",
                            "entity_id": hotel.poi_id,
                            "entity_name": hotel.name,
                            "city": constraints.city,
                            "check_in": constraints.start_date.isoformat(),
                            "check_out": constraints.end_date.isoformat(),
                        },
                        required_fields=["status", "source_url"],
                        critical=False,
                        reason="住宿价格必须与酒店和日期关联",
                    )
                )

        if (PriceCategory.MEAL_PERSON, None) not in existing:
            dining_preference = next(
                (item for item in constraints.preferences if "食" in item or "餐" in item),
                "当地餐饮",
            )
            tasks.append(
                TaskSpec(
                    task_id=f"meal_price_{round_index}",
                    task_type=TaskType.QUERY_MEAL_PRICE,
                    arguments={
                        "quote_id": f"meal:{constraints.city}",
                        "city": constraints.city,
                        "dining_preference": dining_preference,
                    },
                    required_fields=["status", "source_url"],
                    critical=False,
                    reason="餐饮预算使用实时人均价格来源，不让模型猜测",
                )
            )
        return tasks

    def _route_tasks(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        usable_ids: set[str],
        round_index: int,
    ) -> list[TaskSpec]:
        candidates = [poi for poi in evidence.pois if poi.poi_id in usable_ids]
        required = [
            poi
            for poi in candidates
            if any(
                must_visit in poi.name or poi.name in must_visit
                for must_visit in constraints.must_visit
            )
        ]
        required_ids = {poi.poi_id for poi in required}
        optional = sorted(
            (poi for poi in candidates if poi.poi_id not in required_ids),
            key=lambda poi: poi.rating or 0,
            reverse=True,
        )
        limit = max(self.max_candidates, len(required))
        candidates = [*required, *optional[: max(0, limit - len(required))]]
        existing = {
            (route.origin_id, route.destination_id, route.mode)
            for route in evidence.routes
        }

        # k 近邻可以减少 API 调用，但分簇数据可能不连通，因此额外加入一棵
        # 基于直线距离的最小生成树。真实耗时仍由高德查询，不使用直线距离代替。
        undirected_pairs: set[tuple[str, str]] = set()
        for origin in candidates:
            neighbors = sorted(
                (candidate for candidate in candidates if candidate.poi_id != origin.poi_id),
                key=lambda candidate: self._distance(origin, candidate),
            )[: self.nearest_neighbors]
            for destination in neighbors:
                undirected_pairs.add(tuple(sorted((origin.poi_id, destination.poi_id))))

        candidate_by_id = {poi.poi_id: poi for poi in candidates}
        if candidates:
            connected = {candidates[0].poi_id}
            remaining = set(candidate_by_id) - connected
            while remaining:
                origin_id, destination_id = min(
                    (
                        (origin_id, destination_id)
                        for origin_id in connected
                        for destination_id in remaining
                    ),
                    key=lambda pair: self._distance(
                        candidate_by_id[pair[0]],
                        candidate_by_id[pair[1]],
                    ),
                )
                undirected_pairs.add(tuple(sorted((origin_id, destination_id))))
                connected.add(destination_id)
                remaining.remove(destination_id)

        tasks: list[TaskSpec] = []
        for first_id, second_id in sorted(undirected_pairs):
            first = candidate_by_id[first_id]
            second = candidate_by_id[second_id]
            # 路线具有方向性，两个方向都必须有真实耗时证据。
            for route_origin, route_destination in ((first, second), (second, first)):
                if (
                    route_origin.poi_id,
                    route_destination.poi_id,
                    constraints.transportation,
                ) in existing:
                    continue
                tasks.append(
                    TaskSpec(
                        task_id=(
                            f"route_{round_index}_{route_origin.poi_id}_"
                            f"{route_destination.poi_id}"
                        ),
                        task_type=TaskType.QUERY_ROUTE,
                        arguments={
                            "origin_id": route_origin.poi_id,
                            "destination_id": route_destination.poi_id,
                            "origin": route_origin.location.model_dump(),
                            "destination": route_destination.location.model_dump(),
                            "mode": constraints.transportation.value,
                            "city": constraints.city,
                        },
                        required_fields=["distance_meters", "duration_seconds"],
                        critical=True,
                        reason="约束规划必须使用真实交通耗时",
                    )
                )
        return tasks

    @staticmethod
    def _distance(first, second) -> float:
        """用经纬度近似距离筛选值得查询真实路线的相邻景点。"""

        latitude_scale = 111_000
        longitude_scale = latitude_scale * math.cos(
            math.radians((first.location.latitude + second.location.latitude) / 2)
        )
        dx = (first.location.longitude - second.location.longitude) * longitude_scale
        dy = (first.location.latitude - second.location.latitude) * latitude_scale
        return math.hypot(dx, dy)

    @staticmethod
    def _deduplicate(tasks: list[TaskSpec]) -> list[TaskSpec]:
        unique: dict[str, TaskSpec] = {}
        for task in tasks:
            unique[task.task_id] = task
        return list(unique.values())
