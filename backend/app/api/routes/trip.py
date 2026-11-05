"""旅行规划 API 路由。"""

import logging

from fastapi import APIRouter, HTTPException

from app.graph.builder import get_trip_graph
from app.graph.nodes import (
    TripDataSourceError,
    TripPlannerError,
    TripPlanValidationError,
    TripRequestValidationError,
)
from app.models.schemas import TripPlan, TripPlanResponse, TripRequest

router = APIRouter(prefix="/trip", tags=["旅行规划"])
logger = logging.getLogger(__name__)


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
        result = await graph.ainvoke(
            {
                "request": request,
                "retry_count": 0,
                "warnings": [],
            }
        )

        trip_plan = result.get("final_plan")
        if trip_plan is None:
            raise TripPlanValidationError("工作流结束但没有生成有效行程")
        if not isinstance(trip_plan, TripPlan):
            trip_plan = TripPlan.model_validate(trip_plan)

        for warning in result.get("warnings", []):
            logger.warning("Trip workflow degraded: %s", warning)

        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan,
        )
    except TripRequestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
