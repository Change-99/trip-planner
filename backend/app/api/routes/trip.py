"""旅行规划 API 路由。"""

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.graph.builder import get_trip_graph
from app.config import get_settings
from app.graph.nodes import (
    TripDataSourceError,
    TripPlannerError,
    TripPlanValidationError,
    TripRequestValidationError,
)
from app.models.schemas import (
    StoredTripPlanResponse,
    TripJobStarted,
    TripJobStatus,
    TripPlan,
    TripPlanResponse,
    TripReplanRequest,
    TripRequest,
)
from app.planning.jobs import get_planning_job_manager
from app.services.trip_store import get_trip_repository

router = APIRouter(prefix="/trip", tags=["旅行规划"])
logger = logging.getLogger(__name__)
settings = get_settings()


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求，生成详细的旅行计划",
)
async def plan_trip(request: TripRequest):
    """运行一次旅行规划图，并保持原有响应结构。"""

    try:
        graph = get_trip_graph()
        result = await asyncio.wait_for(
            graph.ainvoke(
                {
                    "request": request,
                }
            ),
            timeout=settings.planning_timeout_seconds,
        )

        trip_plan = result.get("final_plan")
        if trip_plan is None:
            limitations = result.get("limitations", [])
            return TripPlanResponse(
                success=False,
                message="现有证据不足，无法生成满足全部硬约束的行程",
                data=None,
                planning_status=result.get("planning_status", "limited"),
                limitations=limitations,
            )
        if not isinstance(trip_plan, TripPlan):
            trip_plan = TripPlan.model_validate(trip_plan)

        plan_id = uuid4().hex
        get_trip_repository().save_plan(plan_id, request, trip_plan)

        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            plan_id=plan_id,
            data=trip_plan,
            planning_status=result.get("planning_status", "feasible"),
            limitations=result.get("limitations", trip_plan.limitations),
        )
    except TripRequestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        logger.error(
            "Trip planning exceeded %.1f seconds",
            settings.planning_timeout_seconds,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                "旅行规划超过后端时间上限；外部地图、价格或模型服务响应过慢，"
                "请稍后重试或减少必去地点数量"
            ),
        ) from exc
    except (TripDataSourceError, TripPlannerError, TripPlanValidationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected trip planning failure")
        raise HTTPException(
            status_code=500,
            detail="旅行规划服务内部错误",
        ) from exc


@router.post(
    "/jobs",
    response_model=TripJobStarted,
    status_code=202,
    summary="提交异步旅行规划任务",
)
async def create_planning_job(request: TripRequest):
    """立即返回任务 ID；前端通过查询状态展示真实 LangGraph 节点。"""

    job = get_planning_job_manager().start(request)
    return TripJobStarted.model_validate(job)


@router.get(
    "/jobs/{job_id}",
    response_model=TripJobStatus,
    summary="查询规划任务进度",
)
async def get_planning_job(job_id: str):
    job = get_trip_repository().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="规划任务不存在")
    return TripJobStatus.model_validate(job)


@router.get(
    "/plans/{plan_id}",
    response_model=StoredTripPlanResponse,
    summary="读取已持久化的旅行计划",
)
async def get_saved_plan(plan_id: str):
    plan = get_trip_repository().get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="旅行计划不存在")
    return StoredTripPlanResponse.model_validate(plan)


@router.post(
    "/plans/{plan_id}/replan",
    response_model=TripJobStarted,
    status_code=202,
    summary="基于编辑约束重新规划",
)
async def replan_saved_trip(plan_id: str, changes: TripReplanRequest):
    """编辑会创建新修订，完整重跑工具、求解器和 Validator。"""

    stored = get_trip_repository().get_plan(plan_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="旅行计划不存在")

    original = TripRequest.model_validate(stored["request"])
    forbidden = list(dict.fromkeys([*original.forbidden_places, *changes.forbidden_places]))
    requested_must = list(dict.fromkeys([*original.must_visit, *changes.must_visit]))
    must_visit = [
        name
        for name in requested_must
        if not any(name in blocked or blocked in name for blocked in forbidden)
    ]
    extra = changes.additional_requirements.strip()
    requirements = original.free_text_input or ""
    if extra:
        requirements = "\n".join(
            part for part in (requirements, f"本次调整要求：{extra}") if part
        )
    revised_request = original.model_copy(
        update={
            "must_visit": must_visit,
            "forbidden_places": forbidden,
            "free_text_input": requirements,
        }
    )
    job = get_planning_job_manager().start(
        revised_request,
        parent_plan_id=plan_id,
        revision=int(stored["revision"]) + 1,
    )
    return TripJobStarted.model_validate(job)


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划工作流是否可构建",
)
async def health_check():
    """返回工作流图的健康状态，不调用外部模型或工具。"""

    graph = get_trip_graph()
    return {
        "status": "healthy",
        "service": "trip-planner",
        "workflow": "LangGraph StateGraph",
        "nodes": len(graph.get_graph().nodes),
    }
