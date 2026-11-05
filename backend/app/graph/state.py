"""旅行规划图中所有节点共享的状态定义。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.models.schemas import TripPlan, TripRequest


class TripState(TypedDict, total=False):
    """在旅行规划工作流各节点之间传递的数据。"""

    request: TripRequest

    attraction_context: str
    weather_context: str
    hotel_context: str

    draft_plan: dict[str, Any]
    final_plan: TripPlan

    validation_error: str
    retry_count: int
    warnings: Annotated[list[str], operator.add]
