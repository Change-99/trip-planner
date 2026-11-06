"""双层规划 LangGraph 的节点、路由和依赖。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langgraph.types import Send
from pydantic import ValidationError

from app.graph.state import TripState
from app.models.planning import (
    BudgetAssessment,
    EvidenceBundle,
    PlanningConstraints,
    PlanningStatus,
    TravelMode,
)
from app.models.schemas import TripRequest
from app.planning.evidence import build_evidence_bundle, check_evidence
from app.planning.advisor import (
    AdviceGenerator,
    GroundedAdviceGenerator,
    ground_advice,
)
from app.planning.budget import calculate_budget
from app.planning.executor import ToolExecutor
from app.planning.formatter import format_trip_plan
from app.planning.optimizer import ItineraryOptimizer
from app.planning.replanner import create_repair_directive
from app.planning.scorer import InterestScorer, LLMInterestScorer
from app.planning.task_planner import TaskPlanner
from app.planning.validator import validate_itinerary


class TripWorkflowError(RuntimeError):
    """旅行规划工作流中可安全返回给 API 的异常。"""


class TripRequestValidationError(TripWorkflowError):
    """请求字段之间存在逻辑冲突。"""


# 保留旧异常名，避免其他模块的导入接口突然失效。
class TripDataSourceError(TripWorkflowError):
    """必要外部数据源出现非预期故障。"""


class TripPlannerError(TripWorkflowError):
    """规划器出现非预期故障。"""


class TripPlanValidationError(TripWorkflowError):
    """最终行程对象无法通过结构校验。"""


@dataclass
class WorkflowDependencies:
    """可注入依赖，让生产环境和离线测试使用同一张图。"""

    task_planner: TaskPlanner = field(default_factory=TaskPlanner)
    executor: ToolExecutor = field(default_factory=ToolExecutor)
    scorer: InterestScorer = field(default_factory=LLMInterestScorer)
    optimizer: ItineraryOptimizer = field(default_factory=ItineraryOptimizer)
    advisor: AdviceGenerator = field(default_factory=GroundedAdviceGenerator)
    max_evidence_retries: int = 2
    max_itinerary_retries: int = 1


def parse_request_node(state: TripState) -> dict[str, Any]:
    """校验旧请求模型，并转换为约束规划使用的结构化约束。"""

    raw_request = state.get("request")
    if raw_request is None:
        raise TripRequestValidationError("缺少旅行规划请求")
    try:
        request = TripRequest.model_validate(raw_request)
    except ValidationError as exc:
        raise TripRequestValidationError(f"旅行请求字段校验失败：{exc}") from exc

    try:
        start_date = date.fromisoformat(request.start_date)
        end_date = date.fromisoformat(request.end_date)
        daily_start = _parse_clock(request.daily_start_time, allow_24=False)
        daily_end = _parse_clock(request.daily_end_time, allow_24=True)
    except (TypeError, ValueError) as exc:
        raise TripRequestValidationError(str(exc)) from exc

    if end_date < start_date:
        raise TripRequestValidationError("结束日期不能早于开始日期")
    actual_days = (end_date - start_date).days + 1
    if request.travel_days != actual_days:
        raise TripRequestValidationError(
            f"travel_days={request.travel_days} 与日期范围的 {actual_days} 天不一致"
        )
    if daily_start >= daily_end:
        raise TripRequestValidationError("每天结束时间必须晚于开始时间")

    forbidden = {name.strip() for name in request.forbidden_places}
    conflicts = [
        name
        for name in request.must_visit
        if any(name in blocked or blocked in name for blocked in forbidden)
    ]
    if conflicts:
        raise TripRequestValidationError(
            "同一地点不能既是必去又是禁止条件：" + "、".join(conflicts)
        )

    constraints = PlanningConstraints(
        city=request.city.strip(),
        start_date=start_date,
        end_date=end_date,
        travel_days=request.travel_days,
        transportation=_travel_mode(request.transportation),
        accommodation=request.accommodation,
        preferences=request.preferences,
        additional_requirements=request.free_text_input or "",
        budget_limit_yuan=request.budget_limit,
        must_visit=request.must_visit,
        forbidden_places=request.forbidden_places,
        forbidden_categories=request.forbidden_categories,
        daily_start_minute=daily_start,
        daily_end_minute=daily_end,
        pace=request.pace,
        traveler_count=request.traveler_count,
        room_count=request.room_count,
    )
    return {
        "request": request,
        "constraints": constraints,
        "task_results": [],
        "evidence": EvidenceBundle(),
        "evidence_retry_count": 0,
        "itinerary_retry_count": 0,
        "excluded_poi_ids": [],
        "planning_status": PlanningStatus.COLLECTING_EVIDENCE.value,
        "limitations": [],
    }


def create_task_planner_node(dependencies: WorkflowDependencies):
    """创建第一层任务规划节点。"""

    def task_planner_node(state: TripState) -> dict[str, Any]:
        plan = dependencies.task_planner.plan(
            constraints=state["constraints"],
            evidence=state.get("evidence", EvidenceBundle()),
            report=state.get("evidence_report"),
            round_index=state.get("evidence_retry_count", 0),
        )
        update: dict[str, Any] = {"task_plan": plan}
        if not plan.tasks:
            update["limitations"] = ["任务规划器无法生成新的数据收集任务"]
        return update

    return task_planner_node


def dispatch_tasks(state: TripState) -> str | list[Send]:
    """把结构化任务并行发送给 Executor。"""

    tasks = state["task_plan"].tasks
    if not tasks:
        return "format_limitation"
    return [Send("execute_task", {"current_task": task}) for task in tasks]


def create_execute_task_node(dependencies: WorkflowDependencies):
    """创建只执行白名单工具任务的节点。"""

    async def execute_task_node(state: TripState) -> dict[str, Any]:
        result = await dependencies.executor.execute(state["current_task"])
        return {"task_results": [result]}

    return execute_task_node


def aggregate_evidence_node(state: TripState) -> dict[str, Any]:
    """将所有工具结果聚合为统一证据包。"""

    evidence = build_evidence_bundle(state.get("task_results", []))
    return {"evidence": evidence}


def create_check_evidence_node(dependencies: WorkflowDependencies):
    """创建证据充分性检查节点。"""

    def check_evidence_node(state: TripState) -> dict[str, Any]:
        retry_count = state.get("evidence_retry_count", 0)
        report = check_evidence(
            state["constraints"],
            state["evidence"],
            retry_count=retry_count,
            max_retries=dependencies.max_evidence_retries,
            max_candidates=dependencies.task_planner.max_candidates,
        )
        update: dict[str, Any] = {"evidence_report": report}
        if report.sufficient:
            update["planning_status"] = PlanningStatus.READY_TO_PLAN.value
        elif report.can_retry:
            update["evidence_retry_count"] = retry_count + 1
        else:
            update["planning_status"] = PlanningStatus.LIMITED.value
            update["limitations"] = report.missing_requirements
        return update

    return check_evidence_node


def route_after_evidence(state: TripState) -> str:
    """证据充足则排程，不足则补查，达到上限则明确失败。"""

    return {
        "plan": "itinerary_planner",
        "supplement": "task_planner",
        "limitation": "format_limitation",
    }[state["evidence_report"].next_action]


def create_itinerary_planner_node(dependencies: WorkflowDependencies):
    """创建兴趣评分加 CP-SAT 约束求解节点。"""

    async def itinerary_planner_node(state: TripState) -> dict[str, Any]:
        excluded = set(state.get("excluded_poi_ids", []))
        usable_ids = [
            poi_id
            for poi_id in state["evidence_report"].usable_poi_ids
            if poi_id not in excluded
        ]
        usable_set = set(usable_ids)
        candidates = [
            poi for poi in state["evidence"].pois if poi.poi_id in usable_set
        ]
        scores = await asyncio.to_thread(
            dependencies.scorer.score,
            state["constraints"],
            candidates,
        )
        itinerary = await asyncio.to_thread(
            dependencies.optimizer.solve,
            state["constraints"],
            state["evidence"],
            usable_ids,
            scores,
        )
        return {
            "candidate_scores": scores,
            "draft_itinerary": itinerary,
            "planning_status": (
                PlanningStatus.INFEASIBLE.value
                if itinerary.solver_status == "infeasible"
                else PlanningStatus.READY_TO_PLAN.value
            ),
            "limitations": itinerary.limitations,
        }

    return itinerary_planner_node


def validate_plan_node(state: TripState) -> dict[str, Any]:
    """独立校验求解结果，不信任上一个节点的自我声明。"""

    report = validate_itinerary(
        state["constraints"],
        state["evidence"],
        state["draft_itinerary"],
    )
    return {"validation_report": report}


def route_after_validation(state: TripState) -> str:
    """校验通过才允许格式化成功响应。"""

    return "calculate_budget" if state["validation_report"].passed else "replanner"


def calculate_budget_node(state: TripState) -> dict[str, Any]:
    """用价格证据计算部分预算，未知项目不会按 0 元处理。"""

    assessment = calculate_budget(
        state["constraints"],
        state["evidence"],
        state["draft_itinerary"],
    )
    return {"budget_assessment": assessment}


def create_generate_advice_node(dependencies: WorkflowDependencies):
    """创建只读取白名单事实的旅行建议节点。"""

    async def generate_advice_node(state: TripState) -> dict[str, Any]:
        draft = await asyncio.to_thread(
            dependencies.advisor.generate,
            state["constraints"],
            state["evidence"],
            state["draft_itinerary"],
            state.get("budget_assessment", BudgetAssessment()),
        )
        return {"advice_draft": draft}

    return generate_advice_node


def ground_advice_node(state: TripState) -> dict[str, Any]:
    """拒绝没有证据引用或凭空增加数字的建议。"""

    return {"grounded_advice": ground_advice(state["advice_draft"])}


def create_replanner_node(dependencies: WorkflowDependencies):
    """创建失败后的补证据或重新排程决策节点。"""

    def replanner_node(state: TripState) -> dict[str, Any]:
        report = state["validation_report"]
        directive = create_repair_directive(
            report,
            evidence_retry_count=state.get("evidence_retry_count", 0),
            itinerary_retry_count=state.get("itinerary_retry_count", 0),
            max_evidence_retries=dependencies.max_evidence_retries,
            max_itinerary_retries=dependencies.max_itinerary_retries,
        )
        update: dict[str, Any] = {"repair_directive": directive}
        if directive.action == "fetch_more_evidence":
            update["evidence_retry_count"] = state.get("evidence_retry_count", 0) + 1
        elif directive.action == "reschedule":
            excluded = [
                related_id
                for violation in report.hard_violations
                for related_id in violation.related_ids
            ]
            if excluded:
                directive.excluded_poi_ids = list(dict.fromkeys(excluded))
                update["excluded_poi_ids"] = directive.excluded_poi_ids
                update["itinerary_retry_count"] = (
                    state.get("itinerary_retry_count", 0) + 1
                )
            else:
                directive.action = "stop"
                directive.reasons.append("违规项无法通过排除候选景点安全修复")
        if directive.action == "stop":
            update["planning_status"] = PlanningStatus.INFEASIBLE.value
            update["limitations"] = directive.reasons
        return update

    return replanner_node


def route_after_replanner(state: TripState) -> str:
    """根据修复指令回到对应节点，硬约束不会被放宽。"""

    return {
        "fetch_more_evidence": "task_planner",
        "reschedule": "itinerary_planner",
        "stop": "format_limitation",
    }[state["repair_directive"].action]


def format_response_node(state: TripState) -> dict[str, Any]:
    """只格式化已经通过 Validator 的行程。"""

    plan = format_trip_plan(
        state["constraints"],
        state["evidence"],
        state["draft_itinerary"],
        state.get("budget_assessment"),
        state.get("grounded_advice"),
    )
    return {
        "final_plan": plan,
        "planning_status": PlanningStatus.FEASIBLE.value,
        "limitations": plan.limitations,
    }


def format_limitation_node(state: TripState) -> dict[str, Any]:
    """整理不可行原因；此节点绝不生成看似完整的假行程。"""

    reasons = list(state.get("limitations", []))
    evidence = state.get("evidence")
    if evidence:
        reasons.extend(evidence.limitations)
    evidence_report = state.get("evidence_report")
    if evidence_report:
        reasons.extend(evidence_report.missing_requirements)
    itinerary = state.get("draft_itinerary")
    if itinerary:
        reasons.extend(itinerary.limitations)
    validation = state.get("validation_report")
    if validation:
        reasons.extend(validation.missing_evidence)
        reasons.extend(item.message for item in validation.hard_violations)
    directive = state.get("repair_directive")
    if directive:
        reasons.extend(directive.reasons)
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    if not reasons:
        reasons = ["现有证据不足，无法在不违反硬约束的前提下生成行程"]

    status = state.get("planning_status", PlanningStatus.LIMITED.value)
    if itinerary and itinerary.solver_status == "infeasible":
        status = PlanningStatus.INFEASIBLE.value
    return {"planning_status": status, "limitations": reasons}


def _parse_clock(value: str, *, allow_24: bool) -> int:
    """把 HH:MM 转成分钟，并拒绝模糊或越界时间。"""

    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"时间“{value}”必须采用 HH:MM 格式") from exc
    if allow_24 and hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间“{value}”超出有效范围")
    return hour * 60 + minute


def _travel_mode(value: str) -> TravelMode:
    """兼容旧前端中文交通方式，并映射为高德路线类型。"""

    normalized = value.strip().lower()
    if any(token in normalized for token in ("步行", "walk")):
        return TravelMode.WALKING
    if any(token in normalized for token in ("驾车", "自驾", "出租", "drive", "taxi")):
        return TravelMode.DRIVING
    if any(token in normalized for token in ("公共", "公交", "地铁", "transit")):
        return TravelMode.TRANSIT
    raise TripRequestValidationError(f"暂不支持交通方式：{value}")
