"""根据校验报告选择补查、重新排程或停止。"""

from app.models.planning import RepairDirective, ValidationReport


def create_repair_directive(
    report: ValidationReport,
    *,
    evidence_retry_count: int,
    itinerary_retry_count: int,
    max_evidence_retries: int,
    max_itinerary_retries: int,
) -> RepairDirective:
    """硬约束不可放宽，只改变下一步处理策略。"""

    reasons = [*report.missing_evidence]
    reasons.extend(violation.message for violation in report.hard_violations)
    if report.repair_target == "evidence" and evidence_retry_count < max_evidence_retries:
        return RepairDirective(action="fetch_more_evidence", reasons=reasons)
    if report.repair_target == "itinerary" and itinerary_retry_count < max_itinerary_retries:
        return RepairDirective(action="reschedule", reasons=reasons)
    return RepairDirective(action="stop", reasons=reasons or ["没有可行修复策略"])
