"""构建旅行规划 StateGraph，并在当前进程中复用编译结果。"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    LegacyTripWorkflowServices,
    TripWorkflowServices,
    create_attraction_node,
    create_hotel_node,
    create_planner_node,
    create_weather_node,
    parse_request_node,
    validate_plan_node,
)
from app.graph.state import TripState


def build_trip_graph(services: TripWorkflowServices | None = None) -> Any:
    """构建并编译旅行规划工作流。"""

    workflow_services = services or LegacyTripWorkflowServices()
    workflow = StateGraph(TripState)

    workflow.add_node("parse_request", parse_request_node)
    workflow.add_node("attractions", create_attraction_node(workflow_services))
    workflow.add_node("weather", create_weather_node(workflow_services))
    workflow.add_node("hotels", create_hotel_node(workflow_services))
    workflow.add_node("planner", create_planner_node(workflow_services))
    workflow.add_node("validator", validate_plan_node)

    workflow.add_edge(START, "parse_request")
    workflow.add_edge("parse_request", "attractions")
    workflow.add_edge("parse_request", "weather")
    workflow.add_edge("parse_request", "hotels")

    # 列表形式的起点表示 AND 汇合：三个独立的数据查询节点全部完成后，
    # planner 节点才会执行一次。
    workflow.add_edge(["attractions", "weather", "hotels"], "planner")
    workflow.add_edge("planner", "validator")
    workflow.add_edge("validator", END)

    return workflow.compile()


_trip_graph: Any | None = None


def get_trip_graph() -> Any:
    """返回当前进程复用的编译图，避免每次请求都重新构建。"""

    global _trip_graph
    if _trip_graph is None:
        _trip_graph = build_trip_graph()
    return _trip_graph
