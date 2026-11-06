"""旧文本生成式旅行规划器的兼容占位模块。

旧实现会让 LLM 直接生成景点、票价和预算，已经被 LangGraph 的工具取证、
CP-SAT 排程、Validator、Price MCP 与 Advice Grounder 完整替代。保留模块名
只是为了让旧代码得到清晰错误，而不是静默回到可能编造数据的路径。
"""


class LegacyTripPlannerDisabledError(RuntimeError):
    """调用方仍在使用已停用的文本生成式规划器。"""


class MultiAgentTripPlanner:
    """已停用；请通过 ``POST /api/trip/plan`` 调用 LangGraph 工作流。"""

    def __init__(self, *args, **kwargs) -> None:
        raise LegacyTripPlannerDisabledError(
            "旧 MultiAgentTripPlanner 已停用：它无法保证开放时间、价格和预算真实性。"
            "请改用 app.graph.builder.get_trip_graph()。"
        )


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """拒绝创建旧规划器，避免重新启用会编造价格的降级路径。"""

    raise LegacyTripPlannerDisabledError(
        "旧旅行规划器已停用，请使用 LangGraph 旅行规划 API。"
    )
