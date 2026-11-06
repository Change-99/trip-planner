"""聚合 Executor 结果并判断证据是否足够。"""

from __future__ import annotations

from datetime import timedelta

from app.models.planning import (
    EvidenceBundle,
    EvidenceReport,
    EvidenceStatus,
    PlanningConstraints,
    POIEvidence,
    PriceCategory,
    PriceQuoteEvidence,
    PriceStatus,
    RouteEvidence,
    TaskResult,
    TaskType,
    TravelMode,
    WeatherEvidence,
    VerificationStatus,
)


def build_evidence_bundle(results: list[TaskResult]) -> EvidenceBundle:
    """把任务结果转换为去重后的领域证据。"""

    pois: dict[str, POIEvidence] = {}
    hotels: dict[str, POIEvidence] = {}
    routes: dict[tuple[str, str, str], RouteEvidence] = {}
    weather: WeatherEvidence | None = None
    price_quotes: dict[str, PriceQuoteEvidence] = {}
    failed_results: list[TaskResult] = []

    for result in results:
        if result.status == EvidenceStatus.FAILED:
            failed_results.append(result)
            continue
        if result.evidence_type in {"pois", "hotels"} and isinstance(result.data, list):
            target = pois if result.evidence_type == "pois" else hotels
            for item in result.data:
                poi = POIEvidence.model_validate(item)
                target[poi.poi_id] = poi
        elif result.evidence_type == "routes" and result.data:
            route = RouteEvidence.model_validate(result.data)
            routes[(route.origin_id, route.destination_id, route.mode.value)] = route
        elif result.evidence_type == "weather" and result.data:
            weather = WeatherEvidence.model_validate(result.data)
        elif result.evidence_type == "price_quotes" and result.data:
            quote = PriceQuoteEvidence.model_validate(result.data)
            price_quotes[quote.quote_id] = quote

    for quote in price_quotes.values():
        if not quote.entity_id:
            continue
        if quote.category == PriceCategory.TICKET and quote.entity_id in pois:
            if _price_rank(quote.price.status) >= _price_rank(
                pois[quote.entity_id].ticket_price.status
            ):
                pois[quote.entity_id].ticket_price = quote.price
        elif quote.category == PriceCategory.HOTEL_NIGHT and quote.entity_id in hotels:
            hotels[quote.entity_id].reference_price = quote.price

    return EvidenceBundle(
        pois=list(pois.values()),
        hotels=list(hotels.values()),
        routes=list(routes.values()),
        weather=weather,
        price_quotes=list(price_quotes.values()),
        limitations=_summarize_failures(failed_results),
    )


def _summarize_failures(results: list[TaskResult]) -> list[str]:
    """把内部工具异常转换为适合展示给用户的限制摘要。"""

    limitations: list[str] = []
    route_failures = [item for item in results if item.task_type == TaskType.QUERY_ROUTE]
    if route_failures:
        quota_limited = any(
            "CUQPS_HAS_EXCEEDED_THE_LIMIT" in (item.error or "")
            for item in route_failures
        )
        if quota_limited:
            limitations.append(
                f"高德接口限流，{len(route_failures)} 条候选交通路线暂未核实"
            )
        else:
            limitations.append(
                f"有 {len(route_failures)} 条候选交通路线未能取得可靠耗时"
            )

    if any(item.task_type == TaskType.QUERY_WEATHER for item in results):
        limitations.append("天气数据暂时不可用，行程未使用天气信息")
    if any(item.task_type == TaskType.SEARCH_HOTEL for item in results):
        limitations.append("住宿查询暂时不可用，仅保留用户的住宿偏好")
    if any(item.task_type == TaskType.SEARCH_POI for item in results):
        limitations.append("部分候选景点查询失败，规划只使用已核实的景点数据")
    price_failures = [
        item
        for item in results
        if item.task_type
        in {
            TaskType.QUERY_TICKET_PRICE,
            TaskType.QUERY_HOTEL_PRICE,
            TaskType.QUERY_MEAL_PRICE,
        }
    ]
    if price_failures:
        limitations.append(
            f"价格研究 MCP 有 {len(price_failures)} 项未取得可靠来源，相关金额保持未知"
        )

    known_types = {
        TaskType.QUERY_ROUTE,
        TaskType.QUERY_WEATHER,
        TaskType.SEARCH_HOTEL,
        TaskType.SEARCH_POI,
        TaskType.QUERY_TICKET_PRICE,
        TaskType.QUERY_HOTEL_PRICE,
        TaskType.QUERY_MEAL_PRICE,
    }
    if any(item.task_type not in known_types for item in results):
        limitations.append("部分外部数据暂时不可用")
    return limitations


def _price_rank(status: PriceStatus) -> int:
    return {
        PriceStatus.UNKNOWN: 0,
        PriceStatus.ESTIMATED_RANGE: 1,
        PriceStatus.VERIFIED: 2,
        PriceStatus.FREE: 2,
    }[status]


def check_evidence(
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    *,
    retry_count: int,
    max_retries: int,
    max_candidates: int = 8,
) -> EvidenceReport:
    """用确定性规则检查硬约束所需证据是否齐全。"""

    eligible: list[POIEvidence] = []
    unusable: list[str] = []
    forbidden_names = {name.strip() for name in constraints.forbidden_places}
    forbidden_categories = {
        category.strip() for category in constraints.forbidden_categories
    }
    trip_dates = [
        constraints.start_date + timedelta(days=offset)
        for offset in range(constraints.travel_days)
    ]

    for poi in evidence.pois:
        if any(
            blocked in poi.name or poi.name in blocked for blocked in forbidden_names
        ) or forbidden_categories.intersection(poi.categories):
            unusable.append(poi.poi_id)
            continue
        opening = poi.opening_hours
        if opening.status != VerificationStatus.VERIFIED:
            unusable.append(poi.poi_id)
            continue
        # 当前没有法定节假日数据源时，带节假日例外的开放时间不能视为硬证据。
        if opening.holiday_exception:
            unusable.append(poi.poi_id)
            continue
        if not any(opening.weekly_windows.get(day.weekday()) for day in trip_dates):
            unusable.append(poi.poi_id)
            continue
        eligible.append(poi)

    # 多个搜索词可能各返回一批 POI。只让路线查询和求解器处理一个有界候选集，
    # 但所有匹配“必去”条件的候选都具有最高保留优先级。
    required = [
        poi
        for poi in eligible
        if any(
            must_visit in poi.name or poi.name in must_visit
            for must_visit in constraints.must_visit
        )
    ]
    required_ids = {poi.poi_id for poi in required}
    optional = sorted(
        (poi for poi in eligible if poi.poi_id not in required_ids),
        key=lambda poi: poi.rating or 0,
        reverse=True,
    )
    # 路线查询量会随候选数快速增长。保留“旅行天数 + 2”个候选用于优化，
    # 在保证每天至少一个景点的同时避免无意义地查询全部搜索结果。
    desired_candidates = min(
        max_candidates,
        max(constraints.travel_days + 2, 2),
    )
    candidate_limit = max(desired_candidates, len(required))
    selected_candidates = [
        *required,
        *optional[: max(0, candidate_limit - len(required))],
    ]
    usable = list(dict.fromkeys(poi.poi_id for poi in selected_candidates))

    missing: list[str] = []
    minimum_candidates = min(max(constraints.travel_days, 2), candidate_limit)
    if len(usable) < minimum_candidates:
        missing.append(
            f"至少需要 {minimum_candidates} 个开放时间已核实的候选景点，当前只有 {len(usable)} 个"
        )

    usable_names = {poi.name for poi in evidence.pois if poi.poi_id in usable}
    for must_visit in constraints.must_visit:
        if not any(must_visit in name or name in must_visit for name in usable_names):
            missing.append(f"必去景点“{must_visit}”缺少可用证据")

    if len(usable) > 1 and not _route_graph_connected(
        usable,
        evidence.routes,
        constraints.transportation,
    ):
        missing.append("候选景点之间的真实交通路线尚未形成连通图")

    sufficient = not missing
    can_retry = not sufficient and retry_count < max_retries
    return EvidenceReport(
        sufficient=sufficient,
        usable_poi_ids=usable,
        unusable_poi_ids=unusable,
        missing_requirements=missing,
        can_retry=can_retry,
        next_action=(
            "plan" if sufficient else "supplement" if can_retry else "limitation"
        ),
    )


def _route_graph_connected(
    poi_ids: list[str],
    routes: list[RouteEvidence],
    mode: TravelMode,
) -> bool:
    """把双向可达性按无向图检查，确保候选集合不是孤立点。"""

    nodes = set(poi_ids)
    adjacency: dict[str, set[str]] = {poi_id: set() for poi_id in nodes}
    for route in routes:
        if (
            route.mode == mode
            and route.origin_id in nodes
            and route.destination_id in nodes
        ):
            adjacency[route.origin_id].add(route.destination_id)
            adjacency[route.destination_id].add(route.origin_id)
    if not nodes:
        return False
    visited: set[str] = set()
    stack = [next(iter(nodes))]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        stack.extend(adjacency[current] - visited)
    return visited == nodes
