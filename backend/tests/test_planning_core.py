"""约束求解器、证据检查器和独立 Validator 的单元测试。"""

from datetime import date

from app.models.planning import (
    CandidateScore,
    EvidenceBundle,
    EvidenceSource,
    EvidenceStatus,
    OptimizedItinerary,
    PlanningConstraints,
    PriceEvidence,
    PriceStatus,
    RouteEvidence,
    ScheduledActivity,
    ScheduledDay,
    TaskResult,
    TaskType,
    TravelMode,
)
from app.planning.evidence import build_evidence_bundle, check_evidence
from app.planning.formatter import format_trip_plan
from app.planning.optimizer import ItineraryOptimizer
from app.planning.task_planner import TaskPlanner
from app.planning.validator import validate_itinerary
from test_trip_graph import make_poi


def make_constraints(**updates) -> PlanningConstraints:
    data = {
        "city": "上海",
        "start_date": date(2026, 11, 10),
        "end_date": date(2026, 11, 10),
        "travel_days": 1,
        "transportation": TravelMode.TRANSIT,
        "accommodation": "舒适型酒店",
        "preferences": ["历史文化"],
        "daily_start_minute": 9 * 60,
        "daily_end_minute": 20 * 60,
        "pace": "normal",
    }
    data.update(updates)
    return PlanningConstraints(**data)


def make_bundle() -> EvidenceBundle:
    first = make_poi("p1", "历史博物馆", 121.470)
    second = make_poi("p2", "城市展览馆", 121.475)
    source = EvidenceSource(provider="fixture", method="route")
    routes = [
        RouteEvidence(
            origin_id="p1",
            destination_id="p2",
            mode=TravelMode.TRANSIT,
            distance_meters=800,
            duration_seconds=600,
            source=source,
        ),
        RouteEvidence(
            origin_id="p2",
            destination_id="p1",
            mode=TravelMode.TRANSIT,
            distance_meters=850,
            duration_seconds=660,
            source=source,
        ),
    ]
    return EvidenceBundle(pois=[first, second], routes=routes)


def test_optimizer_and_validator_enforce_timeline_hard_constraints() -> None:
    constraints = make_constraints()
    evidence = make_bundle()
    scores = [
        CandidateScore(poi_id="p1", interest_score=90),
        CandidateScore(poi_id="p2", interest_score=85),
    ]

    itinerary = ItineraryOptimizer(solver_timeout_seconds=2).solve(
        constraints,
        evidence,
        ["p1", "p2"],
        scores,
    )
    report = validate_itinerary(constraints, evidence, itinerary)

    assert itinerary.solver_status in {"optimal", "feasible"}
    assert report.passed is True
    assert len(itinerary.selected_poi_ids) == 2
    activities = itinerary.days[0].activities
    assert sum(item.activity_type == "meal" for item in activities) == 2
    assert sum(item.activity_type == "transport" for item in activities) == 1
    for previous, current in zip(activities, activities[1:]):
        assert previous.end_minute <= current.start_minute


def test_unknown_price_stays_unknown_in_api_model() -> None:
    constraints = make_constraints()
    evidence = make_bundle()
    itinerary = ItineraryOptimizer(solver_timeout_seconds=2).solve(
        constraints,
        evidence,
        ["p1", "p2"],
        [
            CandidateScore(poi_id="p1", interest_score=90),
            CandidateScore(poi_id="p2", interest_score=85),
        ],
    )

    plan = format_trip_plan(constraints, evidence, itinerary)

    assert plan.budget is None
    assert all(
        attraction.ticket_price is None
        for day in plan.days
        for attraction in day.attractions
    )
    assert any("价格" in limitation for limitation in plan.limitations)


def test_evidence_checker_excludes_forbidden_and_holiday_uncertain_pois() -> None:
    evidence = make_bundle()
    evidence.pois[0].categories = ["夜生活"]
    evidence.pois[1].opening_hours.holiday_exception = True
    constraints = make_constraints(forbidden_categories=["夜生活"])

    report = check_evidence(
        constraints,
        evidence,
        retry_count=2,
        max_retries=2,
    )

    assert report.sufficient is False
    assert set(report.unusable_poi_ids) == {"p1", "p2"}
    assert report.next_action == "limitation"


def test_optimizer_reports_infeasible_when_meal_time_cannot_be_reserved() -> None:
    itinerary = ItineraryOptimizer(solver_timeout_seconds=2).solve(
        make_constraints(daily_end_minute=17 * 60),
        make_bundle(),
        ["p1", "p2"],
        [
            CandidateScore(poi_id="p1", interest_score=90),
            CandidateScore(poi_id="p2", interest_score=85),
        ],
    )

    assert itinerary.solver_status == "infeasible"
    assert "午餐" in itinerary.limitations[0]


def test_zero_budget_is_not_treated_as_missing_budget() -> None:
    evidence = make_bundle()
    for poi in evidence.pois:
        poi.ticket_price = PriceEvidence(
            status=PriceStatus.VERIFIED,
            amount_yuan=50,
        )
    itinerary = ItineraryOptimizer(solver_timeout_seconds=2).solve(
        make_constraints(budget_limit_yuan=0),
        evidence,
        ["p1", "p2"],
        [
            CandidateScore(poi_id="p1", interest_score=90),
            CandidateScore(poi_id="p2", interest_score=85),
        ],
    )

    assert itinerary.score.cost_penalty == 100


def test_validator_rejects_unverified_price_used_as_exact_amount() -> None:
    constraints = make_constraints()
    evidence = EvidenceBundle(pois=[make_bundle().pois[0]])
    itinerary = OptimizedItinerary(
        city="上海",
        solver_status="feasible",
        selected_poi_ids=["p1"],
        days=[
            ScheduledDay(
                date=date(2026, 11, 10),
                activities=[
                    ScheduledActivity(
                        activity_type="attraction",
                        poi_id="p1",
                        name="历史博物馆",
                        start_minute=9 * 60,
                        end_minute=10 * 60 + 30,
                        price=PriceEvidence(
                            status=PriceStatus.UNKNOWN,
                            amount_yuan=50,
                        ),
                    ),
                    ScheduledActivity(
                        activity_type="meal",
                        name="午餐时间",
                        start_minute=12 * 60,
                        end_minute=13 * 60,
                    ),
                    ScheduledActivity(
                        activity_type="meal",
                        name="晚餐时间",
                        start_minute=18 * 60,
                        end_minute=19 * 60,
                    ),
                ],
            )
        ],
    )

    report = validate_itinerary(constraints, evidence, itinerary)

    assert report.passed is False
    assert any(item.code == "unverified_price" for item in report.hard_violations)


def test_candidate_cap_and_route_tasks_still_form_connected_graph() -> None:
    constraints = make_constraints()
    evidence = EvidenceBundle(
        pois=[
            make_poi(f"p{index}", f"候选景点{index}", 121.40 + index * 0.01)
            for index in range(10)
        ]
    )
    report = check_evidence(
        constraints,
        evidence,
        retry_count=0,
        max_retries=2,
        max_candidates=4,
    )
    planner = TaskPlanner(max_candidates=4, nearest_neighbors=0)

    plan = planner.plan(constraints, evidence, report, round_index=1)

    assert len(report.usable_poi_ids) == 3
    route_tasks = [task for task in plan.tasks if task.task_type.value == "query_route"]
    assert len(route_tasks) == 4
    undirected_edges = {
        frozenset((task.arguments["origin_id"], task.arguments["destination_id"]))
        for task in route_tasks
    }
    assert len(undirected_edges) == 2


def test_internal_task_errors_are_summarized_for_users() -> None:
    result = TaskResult(
        task_id="route_1_internal_ids",
        task_type=TaskType.QUERY_ROUTE,
        status=EvidenceStatus.FAILED,
        evidence_type="routes",
        source=EvidenceSource(provider="fixture", method="route"),
        error=(
            "AmapServiceError: 高德 REST API 返回失败状态："
            "CUQPS_HAS_EXCEEDED_THE_LIMIT"
        ),
    )

    evidence = build_evidence_bundle([result])

    assert evidence.limitations == ["高德接口限流，1 条候选交通路线暂未核实"]
    assert "route_1_internal_ids" not in evidence.limitations[0]
    assert "AmapServiceError" not in evidence.limitations[0]
