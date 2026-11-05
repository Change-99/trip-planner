"""LangGraph 旅行规划工作流的单元测试，仅使用模拟服务。"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest

from app.graph.builder import build_trip_graph
from app.graph.nodes import TripDataSourceError, TripPlanValidationError
from app.models.schemas import TripPlan, TripRequest


def make_request() -> TripRequest:
    return TripRequest(
        city="上海",
        start_date="2026-11-10",
        end_date="2026-11-12",
        travel_days=3,
        transportation="公共交通",
        accommodation="舒适型酒店",
        preferences=["历史文化", "城市风光"],
        free_text_input="节奏不要太赶",
    )


def make_valid_draft(request: TripRequest) -> dict[str, Any]:
    start = date.fromisoformat(request.start_date)
    days = []
    weather_info = []
    for offset in range(request.travel_days):
        current_date = (start + timedelta(days=offset)).isoformat()
        days.append(
            {
                "day_index": offset,
                "date": current_date,
                "description": "测试日程",
                "transportation": request.transportation,
                "accommodation": request.accommodation,
                "hotel": None,
                "attractions": [
                    {
                        "name": f"测试景点{offset + 1}",
                        "address": "上海市测试路1号",
                        "location": {"longitude": 121.47, "latitude": 31.23},
                        "visit_duration": 120,
                        "description": "用于单元测试",
                    }
                ],
                "meals": [],
            }
        )
        weather_info.append(
            {
                "date": current_date,
                "day_weather": "晴",
                "night_weather": "多云",
                "day_temp": 22,
                "night_temp": 15,
                "wind_direction": "东风",
                "wind_power": "1-3级",
            }
        )
    return {
        "city": request.city,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "days": days,
        "weather_info": weather_info,
        "overall_suggestions": "单元测试建议",
        "budget": None,
    }


class FakeServices:
    def __init__(self) -> None:
        self.active_queries = 0
        self.max_active_queries = 0
        self.planner_calls = 0
        self.planner_inputs: dict[str, str] | None = None

    async def _query(self, result: str) -> str:
        self.active_queries += 1
        self.max_active_queries = max(self.max_active_queries, self.active_queries)
        await asyncio.sleep(0.03)
        self.active_queries -= 1
        return result

    async def search_attractions(self, request: TripRequest) -> str:
        return await self._query("景点上下文")

    async def get_weather(self, request: TripRequest) -> str:
        return await self._query("天气上下文")

    async def search_hotels(self, request: TripRequest) -> str:
        return await self._query("酒店上下文")

    async def generate_plan(
        self,
        request: TripRequest,
        attraction_context: str,
        weather_context: str,
        hotel_context: str,
    ) -> dict[str, Any]:
        self.planner_calls += 1
        self.planner_inputs = {
            "attractions": attraction_context,
            "weather": weather_context,
            "hotels": hotel_context,
        }
        return make_valid_draft(request)


def invoke_graph(services: FakeServices) -> dict[str, Any]:
    graph = build_trip_graph(services)
    return asyncio.run(
        graph.ainvoke(
            {"request": make_request(), "retry_count": 0, "warnings": []}
        )
    )


def test_graph_compiles_parallelizes_and_joins_all_contexts() -> None:
    services = FakeServices()

    result = invoke_graph(services)

    assert isinstance(result["final_plan"], TripPlan)
    assert services.max_active_queries == 3
    assert services.planner_calls == 1
    assert services.planner_inputs == {
        "attractions": "景点上下文",
        "weather": "天气上下文",
        "hotels": "酒店上下文",
    }


def test_one_compiled_graph_keeps_concurrent_request_state_isolated() -> None:
    services = FakeServices()
    graph = build_trip_graph(services)
    shanghai_request = make_request()
    beijing_request = shanghai_request.model_copy(update={"city": "北京"})

    async def run_both():
        return await asyncio.gather(
            graph.ainvoke(
                {"request": shanghai_request, "retry_count": 0, "warnings": []}
            ),
            graph.ainvoke(
                {"request": beijing_request, "retry_count": 0, "warnings": []}
            ),
        )

    results = asyncio.run(run_both())

    assert [result["final_plan"].city for result in results] == ["上海", "北京"]


class OptionalFailuresServices(FakeServices):
    async def get_weather(self, request: TripRequest) -> str:
        raise ConnectionError("weather unavailable")

    async def search_hotels(self, request: TripRequest) -> str:
        raise TimeoutError("hotel unavailable")


def test_optional_service_failures_degrade_without_faking_data() -> None:
    services = OptionalFailuresServices()

    result = invoke_graph(services)

    assert isinstance(result["final_plan"], TripPlan)
    assert len(result["warnings"]) == 2
    assert "不要编造具体天气" in services.planner_inputs["weather"]
    assert "不要编造酒店" in services.planner_inputs["hotels"]


class AttractionFailureServices(FakeServices):
    async def search_attractions(self, request: TripRequest) -> str:
        raise ConnectionError("attractions unavailable")


def test_core_attraction_failure_is_explicit() -> None:
    graph = build_trip_graph(AttractionFailureServices())

    with pytest.raises(TripDataSourceError, match="核心景点数据查询失败"):
        asyncio.run(
            graph.ainvoke(
                {"request": make_request(), "retry_count": 0, "warnings": []}
            )
        )


class InvalidDraftServices(FakeServices):
    async def generate_plan(
        self,
        request: TripRequest,
        attraction_context: str,
        weather_context: str,
        hotel_context: str,
    ) -> dict[str, Any]:
        draft = make_valid_draft(request)
        draft["days"] = draft["days"][:-1]
        return draft


def test_invalid_llm_draft_fails_business_validation() -> None:
    graph = build_trip_graph(InvalidDraftServices())

    with pytest.raises(TripPlanValidationError, match="应生成 3 天行程"):
        asyncio.run(
            graph.ainvoke(
                {"request": make_request(), "retry_count": 0, "warnings": []}
            )
        )


class PydanticInvalidDraftServices(FakeServices):
    async def generate_plan(
        self,
        request: TripRequest,
        attraction_context: str,
        weather_context: str,
        hotel_context: str,
    ) -> dict[str, Any]:
        draft = make_valid_draft(request)
        del draft["overall_suggestions"]
        return draft


def test_missing_required_field_fails_pydantic_validation() -> None:
    graph = build_trip_graph(PydanticInvalidDraftServices())

    with pytest.raises(TripPlanValidationError, match="overall_suggestions"):
        asyncio.run(
            graph.ainvoke(
                {"request": make_request(), "retry_count": 0, "warnings": []}
            )
        )
