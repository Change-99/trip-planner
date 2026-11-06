"""价格研究 MCP Server。

它只负责调用实时检索/正文提取服务并返回原始来源，不负责判断价格真假。
可信级别和金额解析由主进程中的确定性代码完成。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP


mcp = FastMCP("trip-price-research")
SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"


def _headers() -> dict[str, str]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    headers = {"Content-Type": "application/json", "X-Client-Source": "MCP"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        # Tavily MCP 官方实现允许 search/extract 使用 keyless 模式；配额更低，
        # 失败时主工作流会保持价格 unknown，而不是回退到模型猜测。
        headers["X-Tavily-Access-Mode"] = "keyless"
        headers["X-Client-Source"] = "trip-price-mcp-keyless"
    return headers


def _search(query: str) -> dict:
    try:
        with httpx.Client(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
            response = client.post(
                SEARCH_URL,
                headers=_headers(),
                json={
                    "query": query,
                    "topic": "general",
                    "search_depth": "basic",
                    "max_results": 3,
                    "include_answer": False,
                    "include_raw_content": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        return {
            "query": query,
            "results": [],
            "error": f"Tavily 搜索返回 HTTP {exc.response.status_code}",
            "retryable": exc.response.status_code == 429,
        }
    except (httpx.RequestError, ValueError) as exc:
        return {
            "query": query,
            "results": [],
            "error": f"Tavily 搜索失败：{type(exc).__name__}",
            "retryable": False,
        }
    return {
        "query": query,
        "results": [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("raw_content") or item.get("content", ""),
                "score": item.get("score"),
            }
            for item in payload.get("results", [])
            if isinstance(item, dict)
        ],
    }


@mcp.tool
def search_ticket_price(
    entity_name: str,
    city: str,
    visit_start: str,
    visit_end: str,
) -> dict:
    """检索景点官方票价页面，返回候选来源，不直接声称价格已核实。"""

    dates = visit_start if visit_start == visit_end else f"{visit_start}至{visit_end}"
    return _search(f'{city} "{entity_name}" 官方 门票 票价 {dates}')


@mcp.tool
def search_hotel_price(
    hotel_name: str,
    city: str,
    check_in: str,
    check_out: str,
) -> dict:
    """检索指定日期的酒店每晚价格来源。"""

    return _search(f'{city} "{hotel_name}" {check_in} {check_out} 每晚 房价 预订')


@mcp.tool
def search_meal_price(city: str, dining_preference: str = "") -> dict:
    """检索目的地餐饮人均消费参考来源。"""

    preference = dining_preference.strip() or "当地餐饮"
    return _search(f'{city} "{preference}" 人均消费 价格')


@mcp.tool
def extract_price_source(url: str, entity_name: str) -> dict:
    """提取搜索结果正文，供主进程对金额和来源做确定性校验。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("只允许提取公开 HTTP(S) 来源")
    if not os.getenv("TAVILY_API_KEY", "").strip():
        return {
            "url": url,
            "content": "",
            "error": "未配置 TAVILY_API_KEY，已跳过高配额消耗的正文提取",
        }
    try:
        with httpx.Client(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
            response = client.post(
                EXTRACT_URL,
                headers=_headers(),
                json={
                    "urls": [url],
                    "extract_depth": "basic",
                    "format": "text",
                    "query": f"{entity_name} 价格 票价 人均 房价",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        return {
            "url": url,
            "content": "",
            "error": f"Tavily 正文提取返回 HTTP {exc.response.status_code}",
        }
    except (httpx.RequestError, ValueError) as exc:
        return {
            "url": url,
            "content": "",
            "error": f"Tavily 正文提取失败：{type(exc).__name__}",
        }
    results = payload.get("results", [])
    content = ""
    if results and isinstance(results[0], dict):
        content = str(results[0].get("raw_content") or "")
    return {"url": url, "content": content[:30000]}


if __name__ == "__main__":
    mcp.run(show_banner=False)
