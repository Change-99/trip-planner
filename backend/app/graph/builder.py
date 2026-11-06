"""构建双层规划 StateGraph，并在当前进程中复用编译结果。"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    WorkflowDependencies,
    aggregate_evidence_node,
    calculate_budget_node,
    create_check_evidence_node,
    create_execute_task_node,
    create_itinerary_planner_node,
    create_generate_advice_node,
    create_replanner_node,
    create_task_planner_node,
    dispatch_tasks,
    format_limitation_node,
    format_response_node,
    ground_advice_node,
    parse_request_node,
    route_after_evidence,
    route_after_replanner,
    route_after_validation,
    validate_plan_node,
)
from app.graph.state import TripState


def build_trip_graph(dependencies: WorkflowDependencies | None = None) -> Any:
    """构建并编译“任务规划 + 约束行程规划”工作流。"""

    deps = dependencies or WorkflowDependencies()
    workflow = StateGraph(TripState)

    workflow.add_node("parse_request", parse_request_node)
    workflow.add_node("task_planner", create_task_planner_node(deps))
    workflow.add_node("execute_task", create_execute_task_node(deps))
    workflow.add_node("aggregate_evidence", aggregate_evidence_node)
    workflow.add_node("check_evidence", create_check_evidence_node(deps))
    workflow.add_node("itinerary_planner", create_itinerary_planner_node(deps))
    workflow.add_node("validate_plan", validate_plan_node)
    workflow.add_node("calculate_budget", calculate_budget_node)
    workflow.add_node("generate_advice", create_generate_advice_node(deps))
    workflow.add_node("ground_advice", ground_advice_node)
    workflow.add_node("replanner", create_replanner_node(deps))
    workflow.add_node("format_response", format_response_node)
    workflow.add_node("format_limitation", format_limitation_node)

    workflow.add_edge(START, "parse_request")
    workflow.add_edge("parse_request", "task_planner")
    workflow.add_conditional_edges("task_planner", dispatch_tasks)
    # 多个 Send 分支的 task_results 先由状态 reducer 合并，下一超步只聚合一次。
    workflow.add_edge("execute_task", "aggregate_evidence")
    workflow.add_edge("aggregate_evidence", "check_evidence")
    workflow.add_conditional_edges("check_evidence", route_after_evidence)
    workflow.add_edge("itinerary_planner", "validate_plan")
    workflow.add_conditional_edges("validate_plan", route_after_validation)
    workflow.add_edge("calculate_budget", "generate_advice")
    workflow.add_edge("generate_advice", "ground_advice")
    workflow.add_edge("ground_advice", "format_response")
    workflow.add_conditional_edges("replanner", route_after_replanner)
    workflow.add_edge("format_response", END)
    workflow.add_edge("format_limitation", END)

    return workflow.compile()


_trip_graph: Any | None = None


def get_trip_graph() -> Any:
    """返回当前进程复用的编译图，避免每次请求都重新构建。"""

    global _trip_graph
    if _trip_graph is None:
        _trip_graph = build_trip_graph()
    return _trip_graph
