"""候选景点兴趣评分：LLM 负责偏好理解，算法只使用结构化分数。"""

from __future__ import annotations

import json
import threading
from typing import Protocol

from app.models.planning import CandidateScore, PlanningConstraints, POIEvidence
from app.services.amap_parsers import extract_json_payload


class InterestScorer(Protocol):
    """兴趣评分器接口。"""

    def score(
        self,
        constraints: PlanningConstraints,
        pois: list[POIEvidence],
    ) -> list[CandidateScore]: ...


class HeuristicInterestScorer:
    """无模型环境下可复现的评分器，也作为 LLM 失败时的降级方案。"""

    def score(
        self,
        constraints: PlanningConstraints,
        pois: list[POIEvidence],
    ) -> list[CandidateScore]:
        scores: list[CandidateScore] = []
        for poi in pois:
            searchable = " ".join([poi.name, *poi.categories]).lower()
            matched = [
                preference
                for preference in constraints.preferences
                if preference.lower() in searchable
                or any(token in searchable for token in preference.lower().split())
            ]
            must_visit = any(
                name in poi.name or poi.name in name for name in constraints.must_visit
            )
            mentioned_in_text = bool(
                constraints.additional_requirements
                and poi.name in constraints.additional_requirements
            )
            rating_score = int((poi.rating or 3.5) * 10)
            score = min(
                100,
                rating_score
                + len(matched) * 15
                + (35 if must_visit else 0)
                + (20 if mentioned_in_text else 0),
            )
            reason = "、".join(matched) if matched else "综合评分"
            if must_visit:
                reason = f"必去景点；{reason}"
            elif mentioned_in_text:
                reason = f"补充需求中明确提及；{reason}"
            scores.append(
                CandidateScore(
                    poi_id=poi.poi_id,
                    interest_score=score,
                    reason=reason,
                )
            )
        return scores


class LLMInterestScorer:
    """让 LLM 只做语义匹配评分，不允许它决定时间和路线。"""

    SYSTEM_PROMPT = """你是旅行兴趣匹配分析器。你只负责根据用户偏好给候选景点打0到100分。
不得编造开放时间、价格、路线或坐标。只返回JSON对象：
{"scores":[{"poi_id":"...","interest_score":80,"reason":"..."}]}"""

    def __init__(self, fallback: InterestScorer | None = None) -> None:
        self.fallback = fallback or HeuristicInterestScorer()
        self._agent = None
        self._agent_lock = threading.Lock()

    def _get_agent(self):
        if self._agent is None:
            from hello_agents import SimpleAgent

            from app.services.llm_service import get_llm

            self._agent = SimpleAgent(
                name="旅行兴趣匹配分析器",
                llm=get_llm(),
                system_prompt=self.SYSTEM_PROMPT,
            )
        return self._agent

    def score(
        self,
        constraints: PlanningConstraints,
        pois: list[POIEvidence],
    ) -> list[CandidateScore]:
        fallback_scores = self.fallback.score(constraints, pois)
        try:
            candidates = [
                {
                    "poi_id": poi.poi_id,
                    "name": poi.name,
                    "categories": poi.categories,
                    "rating": poi.rating,
                }
                for poi in pois
            ]
            prompt = (
                f"用户偏好：{constraints.preferences}\n"
                f"补充需求：{constraints.additional_requirements}\n"
                f"必去景点：{constraints.must_visit}\n"
                f"禁止地点：{constraints.forbidden_places}\n"
                f"候选景点：{json.dumps(candidates, ensure_ascii=False)}"
            )
            # SimpleAgent 可能保存对话状态，共享实例必须串行访问。
            with self._agent_lock:
                raw = self._get_agent().run(prompt)
            payload = extract_json_payload(raw)
            raw_scores = payload.get("scores", []) if isinstance(payload, dict) else []
            parsed = [CandidateScore.model_validate(item) for item in raw_scores]
            parsed_by_id = {score.poi_id: score for score in parsed}
            return [parsed_by_id.get(score.poi_id, score) for score in fallback_scores]
        except Exception:
            return fallback_scores
