"""基于白名单事实生成旅行建议，并过滤没有证据引用的内容。"""

from __future__ import annotations

import json
import re
import threading
from typing import Protocol

from app.models.planning import (
    AdviceDraft,
    AdviceFact,
    AdviceItem,
    BudgetAssessment,
    EvidenceBundle,
    GroundedAdvice,
    OptimizedItinerary,
    PlanningConstraints,
    PriceStatus,
)
from app.services.amap_parsers import extract_json_payload


class AdviceGenerator(Protocol):
    def generate(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        itinerary: OptimizedItinerary,
        budget: BudgetAssessment,
    ) -> AdviceDraft: ...


class GroundedAdviceGenerator:
    """LLM 只重写有引用的事实，规则建议始终作为可靠降级结果。"""

    SYSTEM_PROMPT = """你是旅行建议编辑器。只能依据输入的facts生成建议，不能补充常识事实、价格、开放时间、天气或地点。
每条建议必须引用至少一个fact_id。只返回JSON：
{"items":[{"category":"天气/交通/预算/准备","content":"简洁建议","evidence_refs":["fact_id"]}]}
最多6条。不要修改行程，不要输出输入facts中不存在的数字。"""

    def __init__(self) -> None:
        self._agent = None
        self._agent_lock = threading.Lock()

    def _get_agent(self):
        if self._agent is None:
            from hello_agents import SimpleAgent

            from app.services.llm_service import get_llm

            self._agent = SimpleAgent(
                name="有依据的旅行建议生成器",
                llm=get_llm(),
                system_prompt=self.SYSTEM_PROMPT,
            )
        return self._agent

    def generate(
        self,
        constraints: PlanningConstraints,
        evidence: EvidenceBundle,
        itinerary: OptimizedItinerary,
        budget: BudgetAssessment,
    ) -> AdviceDraft:
        facts = build_advice_facts(constraints, evidence, itinerary, budget)
        fallback = build_rule_advice(facts, constraints, evidence, itinerary, budget)
        llm_items: list[AdviceItem] = []
        try:
            prompt = json.dumps(
                {
                    "user_preferences": constraints.preferences,
                    "additional_requirements": constraints.additional_requirements,
                    "facts": [fact.model_dump(mode="json") for fact in facts],
                },
                ensure_ascii=False,
            )
            with self._agent_lock:
                raw = self._get_agent().run(prompt)
            payload = extract_json_payload(raw)
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                llm_items = [AdviceItem.model_validate(item) for item in payload["items"]]
        except Exception:
            llm_items = []

        combined: list[AdviceItem] = []
        seen: set[str] = set()
        for item in [*llm_items, *fallback]:
            key = item.content.strip()
            if key and key not in seen:
                seen.add(key)
                combined.append(item)
        return AdviceDraft(
            planning_summary=(
                "行程已由约束求解器生成，并通过开放时间、活动冲突、"
                "用餐预留和交通衔接校验。"
            ),
            facts=facts,
            items=combined[:8],
        )


def build_advice_facts(
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    itinerary: OptimizedItinerary,
    budget: BudgetAssessment,
) -> list[AdviceFact]:
    facts: list[AdviceFact] = []
    facts.append(
        AdviceFact(
            fact_id="user_pace",
            text=f"用户选择的行程节奏为{constraints.pace}",
            source_type="user_request",
        )
    )
    if constraints.preferences:
        facts.append(
            AdviceFact(
                fact_id="user_preferences",
                text="用户偏好：" + "、".join(constraints.preferences),
                source_type="user_request",
            )
        )

    if evidence.weather:
        for index, forecast in enumerate(evidence.weather.forecasts):
            facts.append(
                AdviceFact(
                    fact_id=f"weather_{index}",
                    text=(
                        f"{forecast.date}白天{forecast.day_weather}，"
                        f"气温{forecast.day_temp}℃；夜间{forecast.night_weather}，"
                        f"气温{forecast.night_temp}℃"
                    ),
                    source_type="amap_weather",
                )
            )

    poi_by_id = {poi.poi_id: poi for poi in evidence.pois}
    for day_index, day in enumerate(itinerary.days):
        attraction_names = [
            activity.name
            for activity in day.activities
            if activity.activity_type == "attraction"
        ]
        facts.append(
            AdviceFact(
                fact_id=f"schedule_{day_index}",
                text=(
                    f"{day.date.isoformat()}安排{len(attraction_names)}个景点："
                    f"{'、'.join(attraction_names)}；当天交通{day.travel_minutes}分钟"
                ),
                source_type="validated_itinerary",
            )
        )
    unknown_tickets = [
        poi_by_id[poi_id].name
        for poi_id in itinerary.selected_poi_ids
        if poi_id in poi_by_id
        and poi_by_id[poi_id].ticket_price.status == PriceStatus.UNKNOWN
    ]
    if unknown_tickets:
        facts.append(
            AdviceFact(
                fact_id="unknown_ticket_prices",
                text="以下景点票价未核实：" + "、".join(unknown_tickets),
                source_type="price_evidence",
            )
        )
    for index, component in enumerate(budget.components):
        amount = (
            f"确定金额{component.confirmed_yuan}元"
            if component.confirmed_yuan is not None
            else f"估算{component.minimum_yuan}至{component.maximum_yuan}元"
        )
        facts.append(
            AdviceFact(
                fact_id=f"budget_{index}",
                text=f"{component.label}：{amount}",
                source_type="price_evidence",
                source_url=component.source_urls[0] if component.source_urls else None,
            )
        )
    if budget.unknown_items:
        facts.append(
            AdviceFact(
                fact_id="budget_unknown",
                text="预算仍缺少：" + "、".join(budget.unknown_items),
                source_type="budget_calculator",
            )
        )
    return facts


def build_rule_advice(
    facts: list[AdviceFact],
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    itinerary: OptimizedItinerary,
    budget: BudgetAssessment,
) -> list[AdviceItem]:
    items: list[AdviceItem] = []
    if evidence.weather:
        rainy_refs = [
            f"weather_{index}"
            for index, item in enumerate(evidence.weather.forecasts)
            if any(token in f"{item.day_weather}{item.night_weather}" for token in ("雨", "雪"))
        ]
        if rainy_refs:
            items.append(
                AdviceItem(
                    category="天气",
                    content="预报包含雨雪天气，建议携带雨具并为室外活动预留调整空间。",
                    evidence_refs=rainy_refs,
                )
            )
    long_days = [
        index
        for index, day in enumerate(itinerary.days)
        if day.travel_minutes >= 60
    ]
    if long_days:
        items.append(
            AdviceItem(
                category="交通",
                content="部分日期的行程内交通时间较长，建议按计划时间出发并关注实时路况。",
                evidence_refs=[f"schedule_{index}" for index in long_days],
            )
        )
    if any(fact.fact_id == "unknown_ticket_prices" for fact in facts):
        items.append(
            AdviceItem(
                category="准备",
                content="部分景点票价尚未核实，出发前请通过景区官方渠道确认票价和预约要求。",
                evidence_refs=["unknown_ticket_prices"],
            )
        )
    if budget.status != "verified":
        refs = [fact.fact_id for fact in facts if fact.fact_id.startswith("budget_")]
        if refs:
            items.append(
                AdviceItem(
                    category="预算",
                    content="当前预算属于部分估算，建议为尚未核实或可能浮动的项目预留余量。",
                    evidence_refs=refs,
                )
            )
    if not items and itinerary.days:
        items.append(
            AdviceItem(
                category="行程",
                content="建议按照已校验的时间轴游览，并在当天根据实际排队情况小幅调整停留时间。",
                evidence_refs=["schedule_0"],
            )
        )
    return items


def ground_advice(draft: AdviceDraft) -> GroundedAdvice:
    """拒绝无引用、引用不存在或凭空增加数字的 LLM 建议。"""

    facts = {fact.fact_id: fact for fact in draft.facts}
    accepted: list[AdviceItem] = []
    rejected = 0
    for item in draft.items:
        refs = list(dict.fromkeys(item.evidence_refs))
        if not refs or any(ref not in facts for ref in refs):
            rejected += 1
            continue
        supporting_text = " ".join(facts[ref].text for ref in refs)
        claimed_numbers = set(re.findall(r"\d+(?:\.\d+)?", item.content))
        supported_numbers = set(re.findall(r"\d+(?:\.\d+)?", supporting_text))
        if not claimed_numbers.issubset(supported_numbers):
            rejected += 1
            continue
        if any(symbol in item.content for symbol in ("元", "¥", "￥")) and not any(
            symbol in supporting_text for symbol in ("元", "¥", "￥")
        ):
            rejected += 1
            continue
        accepted.append(item.model_copy(update={"evidence_refs": refs}))
    return GroundedAdvice(
        planning_summary=draft.planning_summary,
        items=accepted[:6],
        rejected_count=rejected,
    )
