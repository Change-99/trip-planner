"""旅行规划图使用的节点和服务适配器。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from app.graph.state import TripState
from app.models.schemas import TripPlan, TripRequest

logger = logging.getLogger(__name__)


class TripWorkflowError(RuntimeError):
    """工作流预期异常的基类，此类异常信息可以安全返回给 API。"""


class TripRequestValidationError(TripWorkflowError):
    """请求结构合法，但字段之间存在逻辑冲突。"""


class TripDataSourceError(TripWorkflowError):
    """必要的外部数据源调用失败。"""


class TripPlannerError(TripWorkflowError):
    """规划模型调用失败，或者返回了无法解析的内容。"""


class TripPlanValidationError(TripWorkflowError):
    """模型生成的草稿不符合最终旅行计划的数据约束。"""


class TripWorkflowServices(Protocol):
    """工具服务和规划模型的可注入接口。"""

    async def search_attractions(self, request: TripRequest) -> str: ...

    async def get_weather(self, request: TripRequest) -> str: ...

    async def search_hotels(self, request: TripRequest) -> str: ...

    async def generate_plan(
        self,
        request: TripRequest,
        attraction_context: str,
        weather_context: str,
        hotel_context: str,
    ) -> str | Mapping[str, Any]: ...


class LegacyTripWorkflowServices:
    """复用项目现有 Agent 和规划提示词的适配器。

    旧版模块采用延迟导入，因此构建图和运行单元测试时不会创建模型、
    工具客户端，也不需要真实 API Key。
    """

    def __init__(self) -> None:
        self._legacy_planner: Any | None = None
        # SimpleAgent 可能保存可变的对话状态。每个 Agent 使用独立的锁，
        # 避免两个 HTTP 请求同时修改同一个实例，同时仍允许三个不同的
        # 查询 Agent 并行执行。
        self._agent_locks = {
            "attractions": asyncio.Lock(),
            "weather": asyncio.Lock(),
            "hotels": asyncio.Lock(),
            "planner": asyncio.Lock(),
        }

    def _planner(self) -> Any:
        if self._legacy_planner is None:
            from app.agents.trip_planner_agent import get_trip_planner_agent

            self._legacy_planner = get_trip_planner_agent()
        return self._legacy_planner

    async def search_attractions(self, request: TripRequest) -> str:
        planner = self._planner()
        query = planner._build_attraction_query(request)
        async with self._agent_locks["attractions"]:
            return await asyncio.to_thread(planner.attraction_agent.run, query)

    async def get_weather(self, request: TripRequest) -> str:
        planner = self._planner()
        query = (
            f"请查询{request.city}从{request.start_date}到{request.end_date}期间可获得的天气信息。"
            "如果日期超出可靠预报范围，请明确说明不确定性，不要编造天气。"
        )
        async with self._agent_locks["weather"]:
            return await asyncio.to_thread(planner.weather_agent.run, query)

    async def search_hotels(self, request: TripRequest) -> str:
        planner = self._planner()
        preference = request.accommodation or "舒适型酒店"
        query = f"请搜索{request.city}的{preference}，推荐交通便利且适合游客入住的住宿。"
        async with self._agent_locks["hotels"]:
            return await asyncio.to_thread(planner.hotel_agent.run, query)

    async def generate_plan(
        self,
        request: TripRequest,
        attraction_context: str,
        weather_context: str,
        hotel_context: str,
    ) -> str:
        planner = self._planner()
        query = planner._build_planner_query(
            request,
            attraction_context,
            weather_context,
            hotel_context,
        )
        async with self._agent_locks["planner"]:
            return await asyncio.to_thread(planner.planner_agent.run, query)


def parse_request_node(state: TripState) -> dict[str, Any]:
    """规范化请求，并校验日期字段之间的业务规则。"""

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
    except (ValueError, TypeError) as exc:
        raise TripRequestValidationError("出行日期必须是有效的 YYYY-MM-DD 格式") from exc

    if end_date < start_date:
        raise TripRequestValidationError("结束日期不能早于开始日期")

    actual_days = (end_date - start_date).days + 1
    if request.travel_days != actual_days:
        raise TripRequestValidationError(
            f"travel_days={request.travel_days} 与日期范围的 {actual_days} 天不一致"
        )

    return {
        "request": request,
        "retry_count": state.get("retry_count", 0),
        "validation_error": "",
    }


def create_attraction_node(services: TripWorkflowServices):
    async def attraction_node(state: TripState) -> dict[str, str]:
        try:
            result = await services.search_attractions(state["request"])
        except Exception as exc:
            logger.warning("Attraction lookup failed: %s", type(exc).__name__)
            raise TripDataSourceError("核心景点数据查询失败，无法生成可靠行程") from exc

        if not result or not result.strip():
            raise TripDataSourceError("核心景点数据为空，无法生成可靠行程")
        return {"attraction_context": result.strip()}

    return attraction_node


def create_weather_node(services: TripWorkflowServices):
    async def weather_node(state: TripState) -> dict[str, Any]:
        try:
            result = await services.get_weather(state["request"])
            if not result or not result.strip():
                raise ValueError("empty weather result")
            return {"weather_context": result.strip()}
        except Exception as exc:
            logger.warning("Weather lookup degraded: %s", type(exc).__name__)
            return {
                "weather_context": "暂无可靠天气信息，规划时不要编造具体天气。",
                "warnings": ["天气服务暂不可用，行程未使用实时天气信息"],
            }

    return weather_node


def create_hotel_node(services: TripWorkflowServices):
    async def hotel_node(state: TripState) -> dict[str, Any]:
        try:
            result = await services.search_hotels(state["request"])
            if not result or not result.strip():
                raise ValueError("empty hotel result")
            return {"hotel_context": result.strip()}
        except Exception as exc:
            logger.warning("Hotel lookup degraded: %s", type(exc).__name__)
            return {
                "hotel_context": "暂无可靠酒店搜索结果，请只给出住宿区域建议，不要编造酒店。",
                "warnings": ["酒店服务暂不可用，行程未使用实时酒店信息"],
            }

    return hotel_node


def _extract_json_object(raw_output: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_output, Mapping):
        return dict(raw_output)
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise TripPlannerError("规划模型返回了空结果")

    decoder = json.JSONDecoder()
    for index, character in enumerate(raw_output):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw_output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise TripPlannerError("规划模型未返回可解析的 JSON 对象")


def create_planner_node(services: TripWorkflowServices):
    async def planner_node(state: TripState) -> dict[str, dict[str, Any]]:
        try:
            raw_output = await services.generate_plan(
                request=state["request"],
                attraction_context=state["attraction_context"],
                weather_context=state["weather_context"],
                hotel_context=state["hotel_context"],
            )
            draft = _extract_json_object(raw_output)
        except TripPlannerError:
            raise
        except Exception as exc:
            logger.warning("Planner execution failed: %s", type(exc).__name__)
            raise TripPlannerError("旅行规划模型调用失败") from exc
        return {"draft_plan": draft}

    return planner_node


def validate_plan_node(state: TripState) -> dict[str, Any]:
    """将不可信的 LLM 草稿转换为经过校验的 API 响应模型。"""

    try:
        request = state["request"]
        plan = TripPlan.model_validate(state.get("draft_plan"))

        if plan.city != request.city:
            raise ValueError(f"行程城市应为 {request.city}，实际为 {plan.city}")
        if plan.start_date != request.start_date or plan.end_date != request.end_date:
            raise ValueError("行程起止日期与用户请求不一致")
        if len(plan.days) != request.travel_days:
            raise ValueError(
                f"应生成 {request.travel_days} 天行程，实际生成 {len(plan.days)} 天"
            )

        start_date = date.fromisoformat(request.start_date)
        for offset, day_plan in enumerate(plan.days):
            expected_index = offset
            expected_date = (start_date + timedelta(days=offset)).isoformat()
            if day_plan.day_index != expected_index:
                raise ValueError(
                    f"第 {offset + 1} 天的 day_index 应为 {expected_index}"
                )
            if day_plan.date != expected_date:
                raise ValueError(
                    f"第 {offset + 1} 天日期应为 {expected_date}，实际为 {day_plan.date}"
                )
            if not day_plan.attractions:
                raise ValueError(f"第 {offset + 1} 天缺少景点安排")

        weather_degraded = any(
            "天气服务" in warning for warning in state.get("warnings", [])
        )
        if not weather_degraded and len(plan.weather_info) != request.travel_days:
            raise ValueError(
                f"应生成 {request.travel_days} 天天气，实际生成 {len(plan.weather_info)} 天"
            )
        expected_dates = {
            (start_date + timedelta(days=offset)).isoformat()
            for offset in range(request.travel_days)
        }
        actual_weather_dates = {weather.date for weather in plan.weather_info}
        if not actual_weather_dates.issubset(expected_dates):
            raise ValueError("天气日期超出了本次旅行日期范围")

        return {"final_plan": plan, "validation_error": ""}
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise TripPlanValidationError(f"行程结果校验失败：{exc}") from exc
