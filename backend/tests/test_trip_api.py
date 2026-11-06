"""验证现有 POST /api/trip/plan 接口契约的兼容性。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import trip as trip_route
from app.models.schemas import DayPlan, TripPlan
from app.services.trip_store import TripRepository
from test_trip_graph import make_request


class FakeCompiledGraph:
    async def ainvoke(self, state):
        request = state["request"]
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[
                DayPlan(
                    date=request.start_date,
                    day_index=0,
                    description="离线测试行程",
                    transportation=request.transportation,
                    accommodation=request.accommodation,
                )
            ],
            overall_suggestions="已通过约束校验",
            planning_status="feasible",
        )
        return {
            **state,
            "final_plan": plan,
            "planning_status": "feasible",
            "limitations": ["门票价格未知"],
        }


class FakeLimitedGraph:
    async def ainvoke(self, state):
        return {
            **state,
            "planning_status": "limited",
            "limitations": ["缺少已核实开放时间"],
        }


def test_plan_endpoint_keeps_request_and_response_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(trip_route, "get_trip_graph", lambda: FakeCompiledGraph())
    monkeypatch.setattr(
        trip_route,
        "get_trip_repository",
        lambda: TripRepository(tmp_path / "api.db"),
    )
    app = FastAPI()
    app.include_router(trip_route.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/trip/plan",
        json=make_request().model_dump(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["plan_id"]
    assert body["message"] == "旅行计划生成成功"
    assert body["data"]["city"] == "上海"
    assert len(body["data"]["days"]) == 1
    assert body["planning_status"] == "feasible"
    assert body["limitations"] == ["门票价格未知"]


def test_plan_endpoint_returns_business_failure_for_infeasible_plan(monkeypatch) -> None:
    monkeypatch.setattr(trip_route, "get_trip_graph", lambda: FakeLimitedGraph())
    app = FastAPI()
    app.include_router(trip_route.router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/trip/plan", json=make_request().model_dump())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["planning_status"] == "limited"
    assert body["limitations"] == ["缺少已核实开放时间"]
