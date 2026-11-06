"""通过项目自有 MCP Server 收集并校验价格证据。"""

from __future__ import annotations

import asyncio
import re
import threading
import warnings
from datetime import date
from typing import Any
from urllib.parse import urlparse

from app.models.planning import (
    EvidenceSource,
    PriceCategory,
    PriceEvidence,
    PriceQuoteEvidence,
    PriceStatus,
)
from app.services.amap_parsers import extract_json_payload


class PriceMCPError(RuntimeError):
    """价格 MCP 无法提供可解析证据。"""


_price_mcp_tool: Any | None = None


class InMemoryPriceMCPTool:
    """通过 FastMCP 内存传输调用项目自有 Server。

    调用仍经过 MCP 工具发现和参数校验，但不再为每个价格
    查询启动一个 Python/FastMCP 子进程。
    """

    def __init__(self, server: Any) -> None:
        self.server = server

    def run(self, request: dict[str, Any]) -> Any:
        tool_name = str(request.get("tool_name") or "")
        arguments = request.get("arguments") or {}
        if not tool_name:
            raise PriceMCPError("缺少 MCP tool_name")

        async def call_tool() -> Any:
            from fastmcp import Client

            async with Client(self.server) as client:
                result = await client.call_tool(tool_name, arguments)
            if result.is_error:
                raise PriceMCPError(f"{tool_name} 执行失败")
            if result.data is not None:
                return result.data
            if result.structured_content is not None:
                return result.structured_content
            return result.content

        return asyncio.run(call_tool())


def get_price_mcp_tool() -> Any:
    """延迟加载本地领域 MCP Server，并在进程内复用。"""

    global _price_mcp_tool
    if _price_mcp_tool is None:
        # FastMCP 2.x 的可选 JWT 模块会触发 Authlib 迁移提示；本项目
        # 的本地内存 MCP 不使用该模块，因此只屏蔽这一条无关告警。
        from authlib.deprecate import AuthlibDeprecationWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", AuthlibDeprecationWarning)
            from app.mcp.price_server import mcp

        _price_mcp_tool = InMemoryPriceMCPTool(mcp)
    return _price_mcp_tool


_RANGE_PATTERN = re.compile(
    r"(?:[¥￥]\s*)?(\d{1,6}(?:\.\d{1,2})?)\s*"
    r"(?:元\s*)?(?:[-—~～至到])\s*(?:[¥￥]\s*)?"
    r"(\d{1,6}(?:\.\d{1,2})?)\s*元"
)
_EXACT_PATTERNS = (
    re.compile(
        r"(?:票价|门票|成人票|价格|房价|每晚|人均|消费)"
        r"[^。；;\n]{0,24}?(?:人民币|[¥￥])?\s*(\d{1,6}(?:\.\d{1,2})?)\s*元"
    ),
    re.compile(r"(?:人民币|[¥￥])\s*(\d{1,6}(?:\.\d{1,2})?)\s*元?"),
    re.compile(
        r"(\d{1,6}(?:\.\d{1,2})?)\s*元"
        r"[^。；;\n]{0,16}?(?:每人|人均|每晚|/晚|门票|票价)"
    ),
)


def parse_price_evidence(
    text: str,
    *,
    category: PriceCategory,
    source_url: str,
    source_title: str,
    trusted_domains: set[str],
    applicable_date: date | None = None,
) -> PriceEvidence:
    """从来源正文确定性提取人民币金额，不让 LLM 解释数字。"""

    normalized = " ".join(text.split())
    trusted = _is_trusted_source(source_url, trusted_domains)
    source = EvidenceSource(
        provider="price_mcp",
        method="tavily.search+extract",
        confidence=1.0 if trusted else 0.6,
    )
    common = {
        "source_url": source_url or None,
        "source_title": source_title or None,
        "source": source,
        "applicable_date": applicable_date,
        "unit": _unit_for(category),
    }

    ranges = []
    for match in _RANGE_PATTERN.finditer(normalized):
        low, high = float(match.group(1)), float(match.group(2))
        if _plausible(low) and _plausible(high):
            ranges.append((min(low, high), max(low, high), match.group(0)))
    if ranges:
        return PriceEvidence(
            status=PriceStatus.ESTIMATED_RANGE,
            minimum_yuan=min(item[0] for item in ranges),
            maximum_yuan=max(item[1] for item in ranges),
            raw_text=ranges[0][2],
            notes=["来源给出区间或多档价格，不作为确定价格"],
            **common,
        )

    values: list[float] = []
    raw_value = ""
    for pattern in _EXACT_PATTERNS:
        for match in pattern.finditer(normalized):
            value = float(match.group(1))
            if _plausible(value):
                values.append(value)
                raw_value = raw_value or match.group(0)
    unique_values = sorted(set(values))

    if category == PriceCategory.TICKET and not unique_values and re.search(
        r"(?:门票|入场|参观)[^。；;\n]{0,12}(?:免费|免票)|(?:免费|免票)[^。；;\n]{0,12}(?:参观|开放)",
        normalized,
    ):
        return PriceEvidence(
            status=PriceStatus.FREE if trusted else PriceStatus.ESTIMATED_RANGE,
            amount_yuan=0 if trusted else None,
            minimum_yuan=None if trusted else 0,
            maximum_yuan=None if trusted else 0,
            raw_text="免费",
            notes=[] if trusted else ["非白名单来源，仅作为免费信息参考"],
            **common,
        )

    if len(unique_values) == 1:
        value = unique_values[0]
        exact_allowed = trusted and category == PriceCategory.TICKET
        return PriceEvidence(
            status=PriceStatus.VERIFIED if exact_allowed else PriceStatus.ESTIMATED_RANGE,
            amount_yuan=value if exact_allowed else None,
            minimum_yuan=None if exact_allowed else value,
            maximum_yuan=None if exact_allowed else value,
            raw_text=raw_value,
            notes=[] if exact_allowed else ["实时检索价格可能随日期或产品变化，仅作为预算参考"],
            **common,
        )
    if len(unique_values) > 1:
        return PriceEvidence(
            status=PriceStatus.ESTIMATED_RANGE,
            minimum_yuan=min(unique_values),
            maximum_yuan=max(unique_values),
            raw_text=raw_value,
            notes=["来源包含多个价格档位，按区间展示"],
            **common,
        )
    return PriceEvidence(
        status=PriceStatus.UNKNOWN,
        notes=["已找到来源，但没有提取到语义明确的人民币价格"],
        **common,
    )


class PriceMCPService:
    """将 MCP 搜索结果转换为有来源、可审计的 PriceQuoteEvidence。"""

    def __init__(self, *, mcp_tool: Any | None = None, trusted_domains: set[str] | None = None) -> None:
        self._mcp_tool = mcp_tool
        self._lock = threading.Lock()
        if trusted_domains is None:
            from app.config import get_settings

            trusted_domains = set(get_settings().price_trusted_domains_list())
        self.trusted_domains = trusted_domains

    def _tool(self) -> Any:
        if self._mcp_tool is None:
            self._mcp_tool = get_price_mcp_tool()
        return self._mcp_tool

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            raw = self._tool().run(
                {"action": "call_tool", "tool_name": tool_name, "arguments": arguments}
            )
        payload = extract_json_payload(raw)
        if payload is None:
            raise PriceMCPError(f"{tool_name} 返回了无法解析的内容")
        return payload

    def ticket_quote(
        self,
        *,
        quote_id: str,
        entity_id: str,
        entity_name: str,
        city: str,
        visit_start: date,
        visit_end: date,
    ) -> PriceQuoteEvidence:
        return self._research(
            quote_id=quote_id,
            category=PriceCategory.TICKET,
            entity_id=entity_id,
            entity_name=entity_name,
            applicable_date=visit_start if visit_start == visit_end else None,
            tool_name="search_ticket_price",
            arguments={
                "entity_name": entity_name,
                "city": city,
                "visit_start": visit_start.isoformat(),
                "visit_end": visit_end.isoformat(),
            },
        )

    def hotel_quote(self, *, quote_id: str, entity_id: str, entity_name: str, city: str, check_in: date, check_out: date) -> PriceQuoteEvidence:
        return self._research(
            quote_id=quote_id,
            category=PriceCategory.HOTEL_NIGHT,
            entity_id=entity_id,
            entity_name=entity_name,
            applicable_date=check_in,
            tool_name="search_hotel_price",
            arguments={"hotel_name": entity_name, "city": city, "check_in": check_in.isoformat(), "check_out": check_out.isoformat()},
        )

    def meal_quote(self, *, quote_id: str, city: str, dining_preference: str) -> PriceQuoteEvidence:
        return self._research(
            quote_id=quote_id,
            category=PriceCategory.MEAL_PERSON,
            entity_id=None,
            entity_name=dining_preference or f"{city}当地餐饮",
            applicable_date=None,
            tool_name="search_meal_price",
            arguments={"city": city, "dining_preference": dining_preference},
        )

    def _research(
        self,
        *,
        quote_id: str,
        category: PriceCategory,
        entity_id: str | None,
        entity_name: str,
        applicable_date: date | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PriceQuoteEvidence:
        payload = self._call(tool_name, arguments)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results:
            reason = payload.get("error") if isinstance(payload, dict) else None
            raise PriceMCPError(reason or f"没有找到“{entity_name}”的价格来源")

        best_unknown: PriceQuoteEvidence | None = None
        for result in results[:3]:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "")
            title = str(result.get("title") or "")
            snippet = str(result.get("content") or "")
            if not url:
                continue
            content = snippet
            price = parse_price_evidence(
                f"{title}\n{content}",
                category=category,
                source_url=url,
                source_title=title,
                trusted_domains=self.trusted_domains,
                applicable_date=applicable_date,
            )
            # 只有白名单官方来源才进一步提取正文确认。
            # 非白名单搜索片段没有价格时保持 unknown，不浪费
            # extract 配额，也不影响行程时间规划。
            if _is_trusted_source(url, self.trusted_domains):
                try:
                    extracted = self._call(
                        "extract_price_source",
                        {"url": url, "entity_name": entity_name},
                    )
                    if isinstance(extracted, dict) and extracted.get("content"):
                        content = str(extracted["content"])
                        price = parse_price_evidence(
                            f"{title}\n{content}",
                            category=category,
                            source_url=url,
                            source_title=title,
                            trusted_domains=self.trusted_domains,
                            applicable_date=applicable_date,
                        )
                except Exception:
                    pass
            quote = PriceQuoteEvidence(
                quote_id=quote_id,
                category=category,
                entity_id=entity_id,
                entity_name=entity_name,
                price=price,
                source=price.source or EvidenceSource(provider="price_mcp", method=tool_name),
                source_url=url,
                raw_excerpt=(content or snippet)[:500],
            )
            if price.status != PriceStatus.UNKNOWN:
                if category == PriceCategory.MEAL_PERSON:
                    # “当地餐饮”是城市级泛化概念，单篇新闻或单个餐厅
                    # 的人均金额不能代表整个行程的每餐预算。先保留原始
                    # 来源供审计，但不让它进入预算计算。
                    quote.price = price.model_copy(
                        update={
                            "status": PriceStatus.UNKNOWN,
                            "amount_yuan": None,
                            "minimum_yuan": None,
                            "maximum_yuan": None,
                            "notes": [
                                *price.notes,
                                "泛化餐饮预算需要多来源或具体餐厅证据，单一来源不采信",
                            ],
                        }
                    )
                    return quote
                return quote
            best_unknown = best_unknown or quote
        if best_unknown:
            return best_unknown
        raise PriceMCPError(f"价格搜索结果缺少可用来源：{entity_name}")


def _is_trusted_source(url: str, configured_domains: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.endswith(".gov.cn"):
        return True
    return any(host == domain or host.endswith(f".{domain}") for domain in configured_domains)


def _plausible(value: float) -> bool:
    return 0 <= value <= 100_000


def _unit_for(category: PriceCategory) -> str:
    return {
        PriceCategory.TICKET: "人次",
        PriceCategory.HOTEL_NIGHT: "间夜",
        PriceCategory.MEAL_PERSON: "人餐",
        PriceCategory.TRANSPORT: "行程段",
    }[category]


_price_service: PriceMCPService | None = None


def get_price_mcp_service() -> PriceMCPService:
    global _price_service
    if _price_service is None:
        _price_service = PriceMCPService()
    return _price_service
