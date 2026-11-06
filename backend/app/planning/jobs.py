"""异步规划任务调度与 LangGraph 真实节点进度。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings
from app.graph.builder import get_trip_graph
from app.models.schemas import TripPlan, TripRequest
from app.services.trip_store import TripRepository, get_trip_repository

logger = logging.getLogger(__name__)


NODE_PROGRESS: dict[str, tuple[int, str]] = {
    "parse_request": (10, "正在校验需求与显式约束"),
    "task_planner": (20, "Task Planner 正在拆分数据任务"),
    "execute_task": (35, "Executor 正在调用地图、天气和价格工具"),
    "aggregate_evidence": (45, "正在聚合外部数据证据"),
    "check_evidence": (52, "正在检查开放时间与路线证据"),
    "itinerary_planner": (70, "Itinerary Planner 正在求解约束行程"),
    "validate_plan": (82, "Validator 正在检查冲突和硬约束"),
    "replanner": (82, "Replanner 正在修复不可行部分"),
    "calculate_budget": (88, "正在计算有来源的部分预算"),
    "generate_advice": (93, "正在生成基于证据的旅行建议"),
    "ground_advice": (97, "正在校验建议的事实引用"),
    "format_response": (100, "规划完成，正在保存结果"),
    "format_limitation": (100, "已完成证据检查，但无法生成可行行程"),
}


class PlanningJobManager:
    """在 FastAPI 事件循环中运行规划任务，并持久化每个真实节点。"""

    def __init__(self, repository: TripRepository | None = None) -> None:
        self.repository = repository or get_trip_repository()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(
        self,
        request: TripRequest,
        *,
        parent_plan_id: str | None = None,
        revision: int = 1,
    ) -> dict[str, Any]:
        job = self.repository.create_job(
            request,
            parent_plan_id=parent_plan_id,
            revision=revision,
        )
        task = asyncio.create_task(self._run(job, request))
        self._tasks[job["job_id"]] = task
        task.add_done_callback(lambda _: self._tasks.pop(job["job_id"], None))
        return job

    async def _run(self, job: dict[str, Any], request: TripRequest) -> None:
        job_id = str(job["job_id"])
        highest_progress = 1
        final_plan: TripPlan | None = None
        limitations: list[str] = []
        try:
            self.repository.update_job(
                job_id,
                status="running",
                stage="正在启动 LangGraph 工作流",
                progress=highest_progress,
            )
            graph = get_trip_graph()

            async def consume_graph_events() -> None:
                nonlocal highest_progress, final_plan
                async for event in graph.astream(
                    {"request": request},
                    stream_mode="updates",
                ):
                    if not isinstance(event, dict):
                        continue
                    for node_name, update in event.items():
                        progress, stage = NODE_PROGRESS.get(
                            node_name,
                            (highest_progress, f"正在执行 {node_name}"),
                        )
                        highest_progress = max(highest_progress, progress)
                        self.repository.update_job(
                            job_id,
                            status="running",
                            stage=stage,
                            progress=highest_progress,
                        )
                        if isinstance(update, dict):
                            candidate = update.get("final_plan")
                            if candidate is not None:
                                final_plan = (
                                    candidate
                                    if isinstance(candidate, TripPlan)
                                    else TripPlan.model_validate(candidate)
                                )
                            limitations.extend(update.get("limitations") or [])

            await asyncio.wait_for(
                consume_graph_events(),
                timeout=get_settings().planning_timeout_seconds,
            )

            if final_plan is None:
                reason = "、".join(dict.fromkeys(limitations)) or "现有证据不足，无法生成可行行程"
                self.repository.update_job(
                    job_id,
                    status="failed",
                    stage="规划未通过硬约束校验",
                    progress=100,
                    error=reason,
                )
                return

            self.repository.save_plan(
                str(job["plan_id"]),
                request,
                final_plan,
                parent_plan_id=job.get("parent_plan_id"),
                revision=int(job.get("revision") or 1),
            )
            self.repository.update_job(
                job_id,
                status="completed",
                stage="旅行计划已保存",
                progress=100,
            )
        except asyncio.TimeoutError:
            self.repository.update_job(
                job_id,
                status="failed",
                stage="规划超时",
                progress=highest_progress,
                error="外部地图、价格或模型服务响应过慢",
            )
        except Exception as exc:
            logger.exception("Planning job %s failed", job_id)
            self.repository.update_job(
                job_id,
                status="failed",
                stage="规划任务失败",
                progress=highest_progress,
                error=f"{type(exc).__name__}: {exc}",
            )


_job_manager: PlanningJobManager | None = None


def get_planning_job_manager() -> PlanningJobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = PlanningJobManager()
    return _job_manager
