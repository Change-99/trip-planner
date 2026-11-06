"""Executor：根据任务类型调用真实工具，而不是生成查询文案。"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from app.models.planning import (
    EvidenceSource,
    EvidenceStatus,
    TaskResult,
    TaskSpec,
    TaskType,
    TravelMode,
)
from app.models.schemas import Location
from app.services.amap_service import AmapService, get_amap_service
from app.services.price_mcp_service import PriceMCPService, get_price_mcp_service


class ToolExecutor:
    """白名单工具分发器。"""

    def __init__(
        self,
        amap_service: AmapService | None = None,
        price_service: PriceMCPService | None = None,
    ) -> None:
        self._amap_service = amap_service
        self._price_service = price_service

    @property
    def amap(self) -> AmapService:
        if self._amap_service is None:
            self._amap_service = get_amap_service()
        return self._amap_service

    @property
    def prices(self) -> PriceMCPService:
        if self._price_service is None:
            self._price_service = get_price_mcp_service()
        return self._price_service

    async def execute(self, task: TaskSpec) -> TaskResult:
        """执行单个任务，并把异常转换为显式失败结果。"""

        try:
            if task.task_type == TaskType.SEARCH_POI:
                return await self._search_pois(task, evidence_type="pois")
            if task.task_type == TaskType.SEARCH_HOTEL:
                return await self._search_pois(task, evidence_type="hotels")
            if task.task_type == TaskType.QUERY_WEATHER:
                evidence = await asyncio.to_thread(
                    self.amap.get_weather_evidence,
                    str(task.arguments["city"]),
                )
                return TaskResult(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    status=evidence.status,
                    evidence_type="weather",
                    data=evidence.model_dump(mode="json"),
                    source=evidence.source,
                )
            if task.task_type == TaskType.QUERY_ROUTE:
                return await self._query_route(task)
            if task.task_type == TaskType.QUERY_TICKET_PRICE:
                quote = await asyncio.to_thread(
                    self.prices.ticket_quote,
                    quote_id=str(task.arguments["quote_id"]),
                    entity_id=str(task.arguments["entity_id"]),
                    entity_name=str(task.arguments["entity_name"]),
                    city=str(task.arguments["city"]),
                    visit_start=date.fromisoformat(str(task.arguments["visit_start"])),
                    visit_end=date.fromisoformat(str(task.arguments["visit_end"])),
                )
                return self._price_result(task, quote)
            if task.task_type == TaskType.QUERY_HOTEL_PRICE:
                quote = await asyncio.to_thread(
                    self.prices.hotel_quote,
                    quote_id=str(task.arguments["quote_id"]),
                    entity_id=str(task.arguments["entity_id"]),
                    entity_name=str(task.arguments["entity_name"]),
                    city=str(task.arguments["city"]),
                    check_in=date.fromisoformat(str(task.arguments["check_in"])),
                    check_out=date.fromisoformat(str(task.arguments["check_out"])),
                )
                return self._price_result(task, quote)
            if task.task_type == TaskType.QUERY_MEAL_PRICE:
                quote = await asyncio.to_thread(
                    self.prices.meal_quote,
                    quote_id=str(task.arguments["quote_id"]),
                    city=str(task.arguments["city"]),
                    dining_preference=str(task.arguments.get("dining_preference") or ""),
                )
                return self._price_result(task, quote)
            raise ValueError(f"不支持的任务类型：{task.task_type}")
        except Exception as exc:
            return TaskResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=EvidenceStatus.FAILED,
                evidence_type=task.task_type.value,
                data=None,
                source=EvidenceSource(
                    provider="tool_executor",
                    method=task.task_type.value,
                    confidence=0,
                ),
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _search_pois(self, task: TaskSpec, evidence_type: str) -> TaskResult:
        arguments = task.arguments
        pois = await asyncio.to_thread(
            self.amap.search_verified_pois,
            str(arguments["keywords"]),
            str(arguments["city"]),
            True,
            int(arguments.get("limit", 10)),
        )
        source = pois[0].source if pois else EvidenceSource(
            provider="amap",
            method="search_verified_pois",
            confidence=0,
        )
        return TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            status=EvidenceStatus.SUCCESS if pois else EvidenceStatus.FAILED,
            evidence_type=evidence_type,
            data=[poi.model_dump(mode="json") for poi in pois],
            source=source,
            error=None if pois else "没有查询到候选地点",
        )

    async def _query_route(self, task: TaskSpec) -> TaskResult:
        arguments: dict[str, Any] = task.arguments
        route = await asyncio.to_thread(
            self.amap.get_route,
            origin_id=str(arguments["origin_id"]),
            destination_id=str(arguments["destination_id"]),
            origin=Location.model_validate(arguments["origin"]),
            destination=Location.model_validate(arguments["destination"]),
            mode=TravelMode(str(arguments["mode"])),
            city=str(arguments.get("city") or "") or None,
        )
        return TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            status=EvidenceStatus.SUCCESS,
            evidence_type="routes",
            data=route.model_dump(mode="json"),
            source=route.source,
        )

    @staticmethod
    def _price_result(task: TaskSpec, quote) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            status=EvidenceStatus.SUCCESS,
            evidence_type="price_quotes",
            data=quote.model_dump(mode="json"),
            source=quote.source,
        )
