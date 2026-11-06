"""独立校验优化器输出，防止错误时间轴进入最终响应。"""

from __future__ import annotations

from app.models.planning import (
    ConstraintViolation,
    EvidenceBundle,
    OptimizedItinerary,
    PlanningConstraints,
    PriceStatus,
    ValidationReport,
    VerificationStatus,
)


def validate_itinerary(
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    itinerary: OptimizedItinerary,
) -> ValidationReport:
    """重新检查开放时间、冲突、交通、用餐、价格和禁止条件。"""

    violations: list[ConstraintViolation] = []
    missing_evidence: list[str] = []
    poi_by_id = {poi.poi_id: poi for poi in evidence.pois}
    route_by_name = {
        f"{route.origin_id}:{route.destination_id}": route for route in evidence.routes
    }

    if itinerary.solver_status == "infeasible":
        violations.append(
            ConstraintViolation(
                code="solver_infeasible",
                message="约束求解器没有产生可行方案",
            )
        )

    for day in itinerary.days:
        ordered = sorted(day.activities, key=lambda item: (item.start_minute, item.end_minute))
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_minute < previous.end_minute:
                violations.append(
                    ConstraintViolation(
                        code="time_overlap",
                        message=f"{day.date} 的“{previous.name}”与“{current.name}”发生冲突",
                    )
                )

        meals = [item for item in ordered if item.activity_type == "meal"]
        if len(meals) < 2:
            violations.append(
                ConstraintViolation(
                    code="meal_time_missing",
                    message=f"{day.date} 没有完整预留午餐和晚餐时间",
                )
            )

        for activity in ordered:
            if activity.start_minute < constraints.daily_start_minute or activity.end_minute > constraints.daily_end_minute:
                violations.append(
                    ConstraintViolation(
                        code="outside_daily_window",
                        message=f"{activity.name} 超出用户每日可用时间",
                    )
                )
            if activity.activity_type != "attraction" or not activity.poi_id:
                continue
            poi = poi_by_id.get(activity.poi_id)
            if poi is None:
                missing_evidence.append(f"景点 {activity.poi_id} 缺少 POI 证据")
                continue
            if any(
                blocked in poi.name or poi.name in blocked
                for blocked in constraints.forbidden_places
            ) or set(poi.categories).intersection(constraints.forbidden_categories):
                violations.append(
                    ConstraintViolation(
                        code="forbidden_place",
                        message=f"行程包含用户明确禁止的景点：{poi.name}",
                        related_ids=[poi.poi_id],
                    )
                )
            windows = poi.opening_hours.weekly_windows.get(day.date.weekday(), [])
            within_opening = any(
                activity.start_minute >= _minute(window.start)
                and activity.end_minute <= _minute(window.end)
                for window in windows
            )
            if (
                poi.opening_hours.status != VerificationStatus.VERIFIED
                or not within_opening
                or poi.opening_hours.holiday_exception
            ):
                violations.append(
                    ConstraintViolation(
                        code="outside_verified_opening_hours",
                        message=f"{poi.name} 的参观时间没有被开放时间证据覆盖",
                        related_ids=[poi.poi_id],
                    )
                )
            if (
                activity.price.status not in {PriceStatus.VERIFIED, PriceStatus.FREE}
                and activity.price.amount_yuan is not None
            ):
                violations.append(
                    ConstraintViolation(
                        code="unverified_price",
                        message=f"{poi.name} 把未经核实的价格当成了确定价格",
                        related_ids=[poi.poi_id],
                    )
                )

        attraction_activities = [
            activity for activity in ordered if activity.activity_type == "attraction"
        ]
        transports = [item for item in ordered if item.activity_type == "transport"]
        for first, second in zip(attraction_activities, attraction_activities[1:]):
            if not first.poi_id or not second.poi_id:
                continue
            route_key = f"{first.poi_id}:{second.poi_id}"
            route = route_by_name.get(route_key)
            if route is None:
                missing_evidence.append(
                    f"{first.name} 到 {second.name} 缺少真实路线耗时"
                )
                continue
            matching_transport = next(
                (
                    item
                    for item in transports
                    if item.name == f"{first.name} → {second.name}"
                ),
                None,
            )
            if matching_transport is None or (
                matching_transport.end_minute - matching_transport.start_minute
                < route.duration_minutes
            ):
                violations.append(
                    ConstraintViolation(
                        code="transport_time_missing",
                        message=f"{first.name} 到 {second.name} 没有预留足够交通时间",
                    )
                )

    passed = not violations and not missing_evidence
    repair_target = "none" if passed else "evidence" if missing_evidence else "itinerary"
    return ValidationReport(
        passed=passed,
        hard_violations=violations,
        missing_evidence=missing_evidence,
        score=itinerary.score,
        repair_target=repair_target,
    )


def _minute(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)
