"""双层旅行规划图的共享状态。"""

from __future__ import annotations

from typing import Annotated, TypedDict

from app.models.planning import (
    AdviceDraft,
    BudgetAssessment,
    CandidateScore,
    EvidenceBundle,
    EvidenceReport,
    OptimizedItinerary,
    PlanningConstraints,
    RepairDirective,
    TaskPlan,
    TaskResult,
    TaskSpec,
    ValidationReport,
    GroundedAdvice,
)
from app.models.schemas import TripPlan, TripRequest


def merge_task_results(
    current: list[TaskResult] | None,
    updates: list[TaskResult] | None,
) -> list[TaskResult]:
    """合并并行 Executor 的结果，并允许重试覆盖同一任务。"""

    merged = {result.task_id: result for result in current or []}
    for result in updates or []:
        merged[result.task_id] = result
    return list(merged.values())


def merge_unique_strings(
    current: list[str] | None,
    updates: list[str] | None,
) -> list[str]:
    """累积限制信息，同时保持首次出现的顺序。"""

    return list(dict.fromkeys([*(current or []), *(updates or [])]))


class TripState(TypedDict, total=False):
    """在 Task Planner、Executor 和行程规划器之间传递的数据。"""

    request: TripRequest
    constraints: PlanningConstraints

    task_plan: TaskPlan
    current_task: TaskSpec
    task_results: Annotated[list[TaskResult], merge_task_results]

    evidence: EvidenceBundle
    evidence_report: EvidenceReport
    candidate_scores: list[CandidateScore]

    draft_itinerary: OptimizedItinerary
    validation_report: ValidationReport
    budget_assessment: BudgetAssessment
    advice_draft: AdviceDraft
    grounded_advice: GroundedAdvice
    repair_directive: RepairDirective
    excluded_poi_ids: Annotated[list[str], merge_unique_strings]

    evidence_retry_count: int
    itinerary_retry_count: int
    planning_status: str
    limitations: Annotated[list[str], merge_unique_strings]

    final_plan: TripPlan
