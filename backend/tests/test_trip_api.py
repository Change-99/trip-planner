"""验证现有 POST /api/trip/plan 接口契约的兼容性。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import trip as trip_route
from test_trip_graph import make_request, make_valid_draft


class FakeCompiledGraph:
    async def ainvoke(self, state):
        request = state["request"]
        return {
            **state,
            "final_plan": make_valid_draft(request),
            "warnings": [],
        }


def test_plan_endpoint_keeps_request_and_response_shape(monkeypatch) -> None:
    monkeypatch.setattr(trip_route, "get_trip_graph", lambda: FakeCompiledGraph())
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
    assert body["message"] == "旅行计划生成成功"
    assert body["data"]["city"] == "上海"
    assert len(body["data"]["days"]) == 3
