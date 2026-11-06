"""第二层规划：使用 OR-Tools CP-SAT 生成满足硬约束的时间轴。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ortools.sat.python import cp_model

from app.models.planning import (
    CandidateScore,
    EvidenceBundle,
    EvidenceSource,
    OptimizedItinerary,
    PlanningConstraints,
    PriceCategory,
    PriceEvidence,
    PriceStatus,
    ScheduledActivity,
    ScheduledDay,
    ScoreBreakdown,
    TravelMode,
)


class ItineraryOptimizer:
    """把候选景点、开放时间和交通路线转换为约束优化模型。"""

    def __init__(self, *, solver_timeout_seconds: float = 8.0) -> None:
        self.solver_timeout_seconds = solver_timeout_seconds

    def solve(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        usable_poi_ids: list[str],
        candidate_scores: list[CandidateScore],
    ) -> OptimizedItinerary:
        usable_set = set(usable_poi_ids)
        pois = [poi for poi in evidence.pois if poi.poi_id in usable_set]
        if len(pois) < constraints.travel_days:
            return self._infeasible(
                constraints,
                "开放时间已核实的景点数量不足，无法保证每天至少一个活动",
            )

        score_by_id = {score.poi_id: score.interest_score for score in candidate_scores}
        route_by_pair = {
            (route.origin_id, route.destination_id): route
            for route in evidence.routes
            if route.mode == constraints.transportation
        }
        model = cp_model.CpModel()
        day_count = constraints.travel_days
        horizon_start = constraints.daily_start_minute
        horizon_end = constraints.daily_end_minute
        duration = {"relaxed": 120, "normal": 90, "intensive": 75}.get(
            constraints.pace,
            90,
        )
        max_per_day = {"relaxed": 2, "normal": 3, "intensive": 4}.get(
            constraints.pace,
            3,
        )

        selected: dict[tuple[int, int], Any] = {}
        starts: dict[tuple[int, int], Any] = {}
        ends: dict[tuple[int, int], Any] = {}
        day_intervals: dict[int, list[Any]] = {day: [] for day in range(day_count)}
        route_arcs: dict[tuple[int, int, int], Any] = {}

        for day_index in range(day_count):
            current_date = constraints.start_date + timedelta(days=day_index)
            for poi_index, poi in enumerate(pois):
                key = (poi_index, day_index)
                selected[key] = model.new_bool_var(f"select_{poi_index}_{day_index}")
                starts[key] = model.new_int_var(
                    horizon_start,
                    horizon_end,
                    f"start_{poi_index}_{day_index}",
                )
                ends[key] = model.new_int_var(
                    horizon_start,
                    horizon_end,
                    f"end_{poi_index}_{day_index}",
                )
                interval = model.new_optional_interval_var(
                    starts[key],
                    duration,
                    ends[key],
                    selected[key],
                    f"visit_{poi_index}_{day_index}",
                )
                day_intervals[day_index].append(interval)

                windows = poi.opening_hours.weekly_windows.get(current_date.weekday(), [])
                window_choices = []
                for window_index, window in enumerate(windows):
                    open_minute = max(self._minute(window.start), horizon_start)
                    close_minute = min(self._minute(window.end), horizon_end)
                    if close_minute - open_minute < duration:
                        continue
                    choice = model.new_bool_var(
                        f"window_{poi_index}_{day_index}_{window_index}"
                    )
                    model.add(choice <= selected[key])
                    model.add(starts[key] >= open_minute).only_enforce_if(choice)
                    model.add(ends[key] <= close_minute).only_enforce_if(choice)
                    window_choices.append(choice)
                if window_choices:
                    model.add(sum(window_choices) == selected[key])
                else:
                    model.add(selected[key] == 0)

        for poi_index in range(len(pois)):
            model.add(
                sum(selected[(poi_index, day)] for day in range(day_count)) <= 1
            )

        for day_index in range(day_count):
            day_selected = [selected[(index, day_index)] for index in range(len(pois))]
            model.add(sum(day_selected) >= 1)
            model.add(sum(day_selected) <= max_per_day)

            lunch_start = max(12 * 60, horizon_start)
            dinner_start = max(18 * 60, lunch_start + 60)
            if lunch_start + 60 > horizon_end or dinner_start + 60 > horizon_end:
                return self._infeasible(
                    constraints,
                    "每日可用时间不足以同时预留午餐和晚餐",
                )
            day_intervals[day_index].append(
                model.new_fixed_size_interval_var(lunch_start, 60, f"lunch_{day_index}")
            )
            day_intervals[day_index].append(
                model.new_fixed_size_interval_var(dinner_start, 60, f"dinner_{day_index}")
            )

            # 每天建立一个以虚拟节点0为起终点的有向环。未选择景点走自环，
            # 真实景点之间的弧只在已经取得路线证据时创建。
            arcs: list[tuple[int, int, Any]] = []
            for poi_index in range(len(pois)):
                node = poi_index + 1
                present = selected[(poi_index, day_index)]
                arcs.append((node, node, present.Not()))
                from_depot = model.new_bool_var(f"depot_to_{node}_{day_index}")
                to_depot = model.new_bool_var(f"{node}_to_depot_{day_index}")
                model.add(from_depot <= present)
                model.add(to_depot <= present)
                arcs.extend([(0, node, from_depot), (node, 0, to_depot)])

            for origin_index, origin in enumerate(pois):
                for destination_index, destination in enumerate(pois):
                    if origin_index == destination_index:
                        continue
                    route = route_by_pair.get((origin.poi_id, destination.poi_id))
                    if route is None:
                        continue
                    arc = model.new_bool_var(
                        f"arc_{origin_index}_{destination_index}_{day_index}"
                    )
                    model.add(arc <= selected[(origin_index, day_index)])
                    model.add(arc <= selected[(destination_index, day_index)])
                    travel_end = model.new_int_var(
                        horizon_start,
                        horizon_end,
                        f"travel_end_{origin_index}_{destination_index}_{day_index}",
                    )
                    travel_interval = model.new_optional_interval_var(
                        ends[(origin_index, day_index)],
                        route.duration_minutes,
                        travel_end,
                        arc,
                        f"travel_{origin_index}_{destination_index}_{day_index}",
                    )
                    day_intervals[day_index].append(travel_interval)
                    model.add(
                        starts[(destination_index, day_index)] >= travel_end
                    ).only_enforce_if(arc)
                    arcs.append((origin_index + 1, destination_index + 1, arc))
                    route_arcs[(origin_index, destination_index, day_index)] = arc

            model.add_circuit(arcs)
            model.add_no_overlap(day_intervals[day_index])

        for must_visit in constraints.must_visit:
            matching = [
                selected[(index, day)]
                for index, poi in enumerate(pois)
                if must_visit in poi.name or poi.name in must_visit
                for day in range(day_count)
            ]
            if not matching:
                return self._infeasible(
                    constraints,
                    f"必去景点“{must_visit}”没有开放时间和路线均可用的候选",
                )
            model.add(sum(matching) == 1)

        interest_expr = sum(
            score_by_id.get(poi.poi_id, 50) * selected[(index, day)]
            for index, poi in enumerate(pois)
            for day in range(day_count)
        )
        travel_expr = sum(
            route_by_pair[(pois[origin].poi_id, pois[destination].poi_id)].duration_minutes
            * arc
            for (origin, destination, _), arc in route_arcs.items()
        )
        known_cost_terms = []
        for index, poi in enumerate(pois):
            price = poi.ticket_price
            planning_amount = self._planning_amount(price)
            if planning_amount is not None:
                amount = int(planning_amount * constraints.traveler_count)
                known_cost_terms.extend(
                    amount * selected[(index, day)] for day in range(day_count)
                )
        route_cost_terms = []
        route_cost_multiplier = (
            constraints.traveler_count
            if constraints.transportation == TravelMode.TRANSIT
            else 1
        )
        for (origin, destination, _), arc in route_arcs.items():
            route_price = route_by_pair[
                (pois[origin].poi_id, pois[destination].poi_id)
            ].estimated_cost
            route_amount = self._planning_amount(route_price)
            if route_amount is not None:
                route_cost_terms.append(int(route_amount * route_cost_multiplier) * arc)
        fixed_cost = self._fixed_trip_cost(constraints, evidence)
        known_cost_expr = (
            sum(known_cost_terms)
            + sum(route_cost_terms)
            + fixed_cost
        )
        fatigue_expr = sum(
            (duration // 15) * selected[(index, day)]
            for index in range(len(pois))
            for day in range(day_count)
        )
        if constraints.budget_limit_yuan is not None:
            cost_excess = model.new_int_var(0, 1_000_000, "cost_excess")
            model.add(
                cost_excess
                >= known_cost_expr - int(constraints.budget_limit_yuan)
            )
            cost_penalty_expr = cost_excess
        else:
            cost_penalty_expr = known_cost_expr

        model.maximize(
            10 * interest_expr
            - 2 * travel_expr
            - 3 * cost_penalty_expr
            - 2 * fatigue_expr
        )

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.solver_timeout_seconds
        solver.parameters.num_search_workers = 8
        status = solver.solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return self._infeasible(constraints, "约束求解器没有找到可行行程")

        selected_ids: list[str] = []
        days: list[ScheduledDay] = []
        total_interest = 0
        total_travel = 0
        total_known_cost = 0
        total_fatigue = 0
        for day_index in range(day_count):
            current_date = constraints.start_date + timedelta(days=day_index)
            activities: list[ScheduledActivity] = []
            selected_indices: list[int] = []
            for poi_index, poi in enumerate(pois):
                if not solver.boolean_value(selected[(poi_index, day_index)]):
                    continue
                selected_indices.append(poi_index)
                selected_ids.append(poi.poi_id)
                start = solver.value(starts[(poi_index, day_index)])
                end = solver.value(ends[(poi_index, day_index)])
                activities.append(
                    ScheduledActivity(
                        activity_type="attraction",
                        poi_id=poi.poi_id,
                        name=poi.name,
                        start_minute=start,
                        end_minute=end,
                        location=poi.location,
                        price=poi.ticket_price,
                        evidence_source=poi.source,
                    )
                )
                total_interest += score_by_id.get(poi.poi_id, 50)
                planning_amount = self._planning_amount(poi.ticket_price)
                if planning_amount is not None:
                    total_known_cost += int(
                        planning_amount * constraints.traveler_count
                    )

            for (origin_index, destination_index, arc_day), arc in route_arcs.items():
                if arc_day != day_index or not solver.boolean_value(arc):
                    continue
                route = route_by_pair[(
                    pois[origin_index].poi_id,
                    pois[destination_index].poi_id,
                )]
                travel_start = solver.value(ends[(origin_index, day_index)])
                activities.append(
                    ScheduledActivity(
                        activity_type="transport",
                        name=f"{pois[origin_index].name} → {pois[destination_index].name}",
                        start_minute=travel_start,
                        end_minute=travel_start + route.duration_minutes,
                        location=pois[destination_index].location,
                        price=route.estimated_cost,
                        evidence_source=route.source,
                    )
                )
                total_travel += route.duration_minutes
                route_amount = self._planning_amount(route.estimated_cost)
                if route_amount is not None:
                    total_known_cost += int(route_amount * route_cost_multiplier)

            activities.extend(
                [
                    ScheduledActivity(
                        activity_type="meal",
                        name="午餐时间",
                        start_minute=max(12 * 60, horizon_start),
                        end_minute=max(12 * 60, horizon_start) + 60,
                    ),
                    ScheduledActivity(
                        activity_type="meal",
                        name="晚餐时间",
                        start_minute=max(18 * 60, max(12 * 60, horizon_start) + 60),
                        end_minute=max(18 * 60, max(12 * 60, horizon_start) + 60) + 60,
                    ),
                ]
            )
            activities.sort(key=lambda activity: (activity.start_minute, activity.end_minute))
            active_minutes = sum(
                activity.end_minute - activity.start_minute for activity in activities
            )
            total_fatigue += len(selected_indices) * (duration // 15)
            days.append(
                ScheduledDay(
                    date=current_date,
                    activities=activities,
                    travel_minutes=sum(
                        activity.end_minute - activity.start_minute
                        for activity in activities
                        if activity.activity_type == "transport"
                    ),
                    active_minutes=active_minutes,
                )
            )

        total_known_cost += fixed_cost
        effective_budget = (
            total_known_cost
            if constraints.budget_limit_yuan is None
            else int(constraints.budget_limit_yuan)
        )
        cost_penalty = max(0, total_known_cost - effective_budget)
        total_score = (
            10 * total_interest
            - 2 * total_travel
            - 3 * cost_penalty
            - 2 * total_fatigue
        )
        limitations = []
        if any(
            poi.poi_id in selected_ids and poi.ticket_price.status == PriceStatus.UNKNOWN
            for poi in pois
        ):
            limitations.append("部分景点门票价格没有可靠来源，未计入确定预算")
        if any(
            poi.poi_id in selected_ids
            and poi.ticket_price.status == PriceStatus.ESTIMATED_RANGE
            for poi in pois
        ):
            limitations.append("部分价格仅作为区间参与预算软约束，不作为确定价格")
        limitations.append("景点游览时长使用基于行程节奏的算法估算值")
        return OptimizedItinerary(
            city=constraints.city,
            days=days,
            selected_poi_ids=selected_ids,
            score=ScoreBreakdown(
                interest=total_interest,
                transportation=total_travel,
                cost_penalty=cost_penalty,
                fatigue=total_fatigue,
                total=total_score,
            ),
            solver_status="optimal" if status == cp_model.OPTIMAL else "feasible",
            limitations=limitations,
        )

    @staticmethod
    def _minute(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _planning_amount(price: PriceEvidence) -> float | None:
        """软预算可使用估算上界，但不会把它标记成确定价格。"""

        if price.status in {PriceStatus.VERIFIED, PriceStatus.FREE}:
            return price.amount_yuan or 0
        if price.status == PriceStatus.ESTIMATED_RANGE:
            if price.maximum_yuan is not None:
                return price.maximum_yuan
            if price.amount_yuan is not None:
                return price.amount_yuan
        return None

    @classmethod
    def _fixed_trip_cost(
        cls,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
    ) -> int:
        """汇总不随景点选择变化的住宿与餐饮参考成本。"""

        total = 0.0
        hotel_quote = next(
            (
                quote
                for quote in evidence.price_quotes
                if quote.category == PriceCategory.HOTEL_NIGHT
            ),
            None,
        )
        if hotel_quote:
            amount = cls._planning_amount(hotel_quote.price)
            if amount is not None:
                total += amount * max(0, constraints.travel_days - 1) * constraints.room_count
        meal_quote = next(
            (
                quote
                for quote in evidence.price_quotes
                if quote.category == PriceCategory.MEAL_PERSON
            ),
            None,
        )
        if meal_quote:
            amount = cls._planning_amount(meal_quote.price)
            if amount is not None:
                total += amount * 2 * constraints.travel_days * constraints.traveler_count
        return int(total)

    @staticmethod
    def _infeasible(
        constraints: PlanningConstraints,
        reason: str,
    ) -> OptimizedItinerary:
        return OptimizedItinerary(
            city=constraints.city,
            solver_status="infeasible",
            limitations=[reason],
        )
