"""SQLite 持久化和 LangGraph 真实任务进度测试。"""

from __future__ import annotations

import asyncio

from app.models.schemas import DayPlan, TripPlan
from app.planning.jobs import PlanningJobManager
from app.services.trip_store import TripRepository
from test_trip_graph import make_request


def make_plan() -> TripPlan:
    return TripPlan(
        city="上海",
        start_date="2026-11-10",
        end_date="2026-11-10",
        days=[
            DayPlan(
                date="2026-11-10",
                day_index=0,
                description="经约束求解的行程",
                transportation="transit",
                accommodation="舒适型酒店",
            )
        ],
        overall_suggestions="已通过校验",
    )


def test_repository_persists_plan_and_request(tmp_path) -> None:
    repository = TripRepository(tmp_path / "trip.db")
    request = make_request()
    repository.save_plan("plan-1", request, make_plan())

    stored = repository.get_plan("plan-1")

    assert stored is not None
    assert stored["request"]["city"] == "上海"
    assert stored["data"]["overall_suggestions"] == "已通过校验"
    assert stored["revision"] == 1


def test_job_manager_records_real_graph_nodes_and_saves_result(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeStreamingGraph:
        async def astream(self, state, stream_mode):
            assert stream_mode == "updates"
            yield {"parse_request": {"request": state["request"]}}
            yield {"task_planner": {"task_plan": {}}}
            yield {"execute_task": {"task_results": []}}
            yield {"itinerary_planner": {"draft_itinerary": {}}}
            yield {"validate_plan": {"validation_report": {}}}
            yield {"format_response": {"final_plan": make_plan()}}

    monkeypatch.setattr(
        "app.planning.jobs.get_trip_graph",
        lambda: FakeStreamingGraph(),
    )
    repository = TripRepository(tmp_path / "jobs.db")
    manager = PlanningJobManager(repository)

    async def run_job():
        job = manager.start(make_request())
        await manager._tasks[job["job_id"]]
        return job

    job = asyncio.run(run_job())
    status = repository.get_job(job["job_id"])
    stored = repository.get_plan(job["plan_id"])

    assert status is not None
    assert status["status"] == "completed"
    assert status["progress"] == 100
    assert status["stage"] == "旅行计划已保存"
    assert stored is not None
    assert stored["data"]["city"] == "上海"
