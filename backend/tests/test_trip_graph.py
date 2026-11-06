"""双层 LangGraph 工作流的离线端到端测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.graph.builder import build_trip_graph
from app.graph.nodes import TripRequestValidationError, WorkflowDependencies
from app.models.planning import (
    EvidenceSource,
    EvidenceStatus,
    OpeningHoursEvidence,
    POIEvidence,
    PriceCategory,
    PriceEvidence,
    PriceQuoteEvidence,
    PriceStatus,
    RouteEvidence,
    TaskResult,
    TaskSpec,
    TaskType,
    TimeWindow,
    TravelMode,
    VerificationStatus,
    WeatherEvidence,
)
from app.models.schemas import Location, TripPlan, TripRequest, WeatherInfo
from app.planning.optimizer import ItineraryOptimizer
from app.planning.scorer import HeuristicInterestScorer


def make_request(**updates) -> TripRequest:
    data = {
        "city": "上海",
        "start_date": "2026-11-10",
        "end_date": "2026-11-10",
        "travel_days": 1,
        "transportation": "公共交通",
        "accommodation": "舒适型酒店",
        "preferences": ["历史文化"],
        "free_text_input": "节奏不要太赶",
    }
    data.update(updates)
    return TripRequest(**data)


def make_poi(
    poi_id: str,
    name: str,
    longitude: float,
    *,
    category: str = "历史文化",
) -> POIEvidence:
    return POIEvidence(
        poi_id=poi_id,
        name=name,
        address=f"上海市{name}路1号",
        location=Location(longitude=longitude, latitude=31.23),
        categories=[category],
        rating=4.7,
        opening_hours=OpeningHoursEvidence(
            status=VerificationStatus.VERIFIED,
            weekly_windows={
                weekday: [TimeWindow(start="09:00", end="20:00")]
                for weekday in range(7)
            },
        ),
        ticket_price=PriceEvidence(),
        source=EvidenceSource(provider="fixture", method="poi_search"),
    )


class FakeExecutor:
    """模拟真实工具，但仍返回 Executor 约定的结构化证据。"""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all
        self.active = 0
        self.max_active = 0
        self.executed_types: list[TaskType] = []
        self.pois = [
            make_poi("p1", "上海历史博物馆", 121.470),
            make_poi("p2", "城市历史展览馆", 121.475),
        ]

    async def execute(self, task: TaskSpec) -> TaskResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.executed_types.append(task.task_type)
        await asyncio.sleep(0.01)
        self.active -= 1
        source = EvidenceSource(provider="fixture", method=task.task_type.value)
        if self.fail_all:
            return TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=EvidenceStatus.FAILED,
                evidence_type=task.task_type.value,
                source=source,
                error="fixture unavailable",
            )
        if task.task_type == TaskType.SEARCH_POI:
            data = [poi.model_dump(mode="json") for poi in self.pois]
            return self._result(task, "pois", data)
        if task.task_type == TaskType.SEARCH_HOTEL:
            hotel = make_poi("h1", "测试酒店", 121.472, category="酒店")
            return self._result(task, "hotels", [hotel.model_dump(mode="json")])
        if task.task_type == TaskType.QUERY_WEATHER:
            weather = WeatherEvidence(
                city="上海",
                forecasts=[WeatherInfo(date="2026-11-10", day_weather="晴")],
                status=EvidenceStatus.SUCCESS,
                source=source,
            )
            return self._result(task, "weather", weather.model_dump(mode="json"))
        if task.task_type == TaskType.QUERY_ROUTE:
            arguments = task.arguments
            route = RouteEvidence(
                origin_id=arguments["origin_id"],
                destination_id=arguments["destination_id"],
                mode=TravelMode(arguments["mode"]),
                distance_meters=800,
                duration_seconds=10 * 60,
                source=source,
            )
            return self._result(task, "routes", route.model_dump(mode="json"))
        if task.task_type in {
            TaskType.QUERY_TICKET_PRICE,
            TaskType.QUERY_HOTEL_PRICE,
            TaskType.QUERY_MEAL_PRICE,
        }:
            category = {
                TaskType.QUERY_TICKET_PRICE: PriceCategory.TICKET,
                TaskType.QUERY_HOTEL_PRICE: PriceCategory.HOTEL_NIGHT,
                TaskType.QUERY_MEAL_PRICE: PriceCategory.MEAL_PERSON,
            }[task.task_type]
            if category == PriceCategory.TICKET:
                price = PriceEvidence(
                    status=PriceStatus.VERIFIED,
                    amount_yuan=50,
                    source_url="https://example.gov.cn/ticket",
                    source=source,
                )
            else:
                price = PriceEvidence(
                    status=PriceStatus.ESTIMATED_RANGE,
                    minimum_yuan=30 if category == PriceCategory.MEAL_PERSON else 300,
                    maximum_yuan=50 if category == PriceCategory.MEAL_PERSON else 400,
                    source_url="https://example.com/price",
                    source=source,
                )
            quote = PriceQuoteEvidence(
                quote_id=task.arguments["quote_id"],
                category=category,
                entity_id=task.arguments.get("entity_id"),
                entity_name=task.arguments.get("entity_name", "当地餐饮"),
                price=price,
                source=source,
                source_url=price.source_url,
            )
            return self._result(task, "price_quotes", quote.model_dump(mode="json"))
        raise AssertionError(f"unexpected task {task.task_type}")

    @staticmethod
    def _result(task: TaskSpec, evidence_type: str, data) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            status=EvidenceStatus.SUCCESS,
            evidence_type=evidence_type,
            data=data,
            source=EvidenceSource(provider="fixture", method=task.task_type.value),
        )


def make_dependencies(executor: FakeExecutor) -> WorkflowDependencies:
    return WorkflowDependencies(
        executor=executor,
        scorer=HeuristicInterestScorer(),
        optimizer=ItineraryOptimizer(solver_timeout_seconds=2),
        max_evidence_retries=2,
        max_itinerary_retries=1,
    )


def invoke_graph(executor: FakeExecutor, request: TripRequest | None = None):
    graph = build_trip_graph(make_dependencies(executor))
    return asyncio.run(graph.ainvoke({"request": request or make_request()}))


def test_graph_collects_real_evidence_then_generates_valid_timeline() -> None:
    executor = FakeExecutor()

    result = invoke_graph(executor)

    assert isinstance(result["final_plan"], TripPlan)
    assert result["planning_status"] == "feasible"
    assert result["validation_report"].passed is True
    assert result["evidence_retry_count"] == 1
    assert executor.max_active >= 2
    assert TaskType.SEARCH_POI in executor.executed_types
    assert TaskType.QUERY_ROUTE in executor.executed_types
    assert TaskType.QUERY_TICKET_PRICE in executor.executed_types
    assert len(result["final_plan"].days[0].attractions) == 2
    assert result["final_plan"].travel_advice
    assert result["final_plan"].budget is not None
    assert any(
        item["type"] == "transport"
        for item in result["final_plan"].days[0].timeline
    )


def test_data_source_failure_returns_limitations_without_fake_plan() -> None:
    result = invoke_graph(FakeExecutor(fail_all=True))

    assert "final_plan" not in result
    assert result["planning_status"] == "limited"
    assert result["limitations"]
    assert any("候选景点" in reason for reason in result["limitations"])


def test_conflicting_must_visit_and_forbidden_place_is_rejected() -> None:
    request = make_request(must_visit=["外滩"], forbidden_places=["外滩"])
    graph = build_trip_graph(make_dependencies(FakeExecutor()))

    with pytest.raises(TripRequestValidationError, match="既是必去又是禁止"):
        asyncio.run(graph.ainvoke({"request": request}))


def test_compiled_graph_keeps_concurrent_request_state_isolated() -> None:
    executor = FakeExecutor()
    graph = build_trip_graph(make_dependencies(executor))

    async def run_both():
        return await asyncio.gather(
            graph.ainvoke({"request": make_request()}),
            graph.ainvoke({"request": make_request(city="北京")}),
        )

    first, second = asyncio.run(run_both())

    assert first["final_plan"].city == "上海"
    assert second["final_plan"].city == "北京"
