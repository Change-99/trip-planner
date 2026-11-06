"""高德地图结构化数据服务。

优先复用项目现有的高德 MCP 工具；当 MCP 响应缺少结构化字段时，
允许回退到高德官方 REST API。所有公开方法都返回经过 Pydantic
校验的领域对象，不再用空列表掩盖解析错误。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from collections.abc import Callable
from typing import Any

import httpx

from app.models.planning import (
    EvidenceSource,
    EvidenceStatus,
    POIEvidence,
    RouteEvidence,
    TravelMode,
    WeatherEvidence,
)
from app.models.schemas import Location, POIInfo, WeatherInfo
from app.services.amap_parsers import (
    extract_json_payload,
    parse_location,
    parse_poi_evidence,
    parse_route_evidence,
    parse_weather_infos,
)

logger = logging.getLogger(__name__)


class AmapServiceError(RuntimeError):
    """高德数据无法获取或无法解析。"""


_amap_mcp_tool: Any | None = None


def get_amap_mcp_tool() -> Any:
    """延迟创建高德 MCP 工具，避免测试阶段强制加载 HelloAgents。"""

    global _amap_mcp_tool
    if _amap_mcp_tool is None:
        from app.config import get_settings

        settings = get_settings()
        if not settings.amap_api_key:
            raise AmapServiceError("AMAP_API_KEY 未配置")

        from hello_agents.tools import MCPTool

        _amap_mcp_tool = MCPTool(
            name="amap",
            description="高德地图服务，支持 POI、路线和天气查询",
            server_command=["uvx", "amap-mcp-server"],
            env={"AMAP_MAPS_API_KEY": settings.amap_api_key},
            auto_expand=True,
        )
    return _amap_mcp_tool


class AmapService:
    """把高德 MCP/REST 数据转换为约束规划可用的结构化证据。"""

    POI_SEARCH_URL = "https://restapi.amap.com/v5/place/text"
    POI_DETAIL_URL = "https://restapi.amap.com/v5/place/detail"
    WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
    GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
    ROUTE_URLS = {
        TravelMode.WALKING: "https://restapi.amap.com/v3/direction/walking",
        TravelMode.DRIVING: "https://restapi.amap.com/v3/direction/driving",
        TravelMode.TRANSIT: "https://restapi.amap.com/v3/direction/transit/integrated",
    }
    QPS_LIMIT_ERRORS = {"CUQPS_HAS_EXCEEDED_THE_LIMIT"}

    def __init__(
        self,
        *,
        mcp_tool: Any | None = None,
        http_client: Any | None = None,
        api_key: str | None = None,
        mcp_enabled: bool | None = None,
        max_qps: float | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mcp_tool = mcp_tool
        self._http_client = http_client
        self._mcp_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        from app.config import get_settings

        settings = get_settings()
        if mcp_enabled is None:
            mcp_enabled = mcp_tool is not None or settings.amap_mcp_enabled
        if max_qps is None:
            # 注入客户端通常用于离线测试或调用方自带限流；生产环境的
            # 真实 httpx 客户端才使用项目级 QPS 限制。
            max_qps = 0.0 if http_client is not None else settings.amap_max_qps
        if max_retries is None:
            max_retries = settings.amap_max_retries
        self._mcp_enabled = mcp_enabled
        self._minimum_interval = 1.0 / max_qps if max_qps > 0 else 0.0
        self._max_retries = max(0, max_retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleep = sleep
        self._monotonic = monotonic
        self._route_cache: dict[tuple[Any, ...], RouteEvidence] = {}
        self._route_cache_lock = threading.Lock()
        if api_key is None:
            api_key = settings.amap_api_key
        self.api_key = api_key

    def _get_mcp_tool(self) -> Any:
        if self._mcp_tool is None:
            self._mcp_tool = get_amap_mcp_tool()
        return self._mcp_tool

    def _call_mcp(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._mcp_enabled:
            raise AmapServiceError("高德 MCP 未开启")
        with self._mcp_lock:
            result = self._get_mcp_tool().run(
                {
                    "action": "call_tool",
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
            )
        payload = extract_json_payload(result)
        if payload is None:
            raise AmapServiceError(f"{tool_name} 返回了无法解析的内容")
        return payload

    def _wait_for_rate_slot(self) -> None:
        """跨并发任务限制 REST 请求启动频率，避免瞬时击穿高德 QPS。"""

        if self._minimum_interval <= 0:
            return
        with self._rate_lock:
            now = self._monotonic()
            scheduled_at = max(now, self._next_request_at)
            self._next_request_at = scheduled_at + self._minimum_interval
        wait_seconds = scheduled_at - now
        if wait_seconds > 0:
            self._sleep(wait_seconds)

    def _retry_delay(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            self._sleep(delay)

    def _rest_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise AmapServiceError("AMAP_API_KEY 未配置，无法调用高德 REST API")

        request_params = {**params, "key": self.api_key, "output": "json"}
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._wait_for_rate_slot()
            owns_client = self._http_client is None
            client = self._http_client or httpx.Client(
                timeout=httpx.Timeout(8.0, connect=3.0)
            )
            try:
                response = client.get(url, params=request_params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                retryable = isinstance(
                    exc,
                    (httpx.TimeoutException, httpx.NetworkError),
                )
                if isinstance(exc, httpx.HTTPStatusError):
                    retryable = exc.response.status_code in {
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                if retryable and attempt < self._max_retries:
                    logger.warning(
                        "高德 REST 临时失败（%s），将重试 %s/%s",
                        type(exc).__name__,
                        attempt + 1,
                        self._max_retries,
                    )
                    self._retry_delay(attempt)
                    continue
                raise AmapServiceError(
                    f"高德 REST API 请求失败：{type(exc).__name__}"
                ) from exc
            finally:
                if owns_client:
                    client.close()

            if not isinstance(payload, Mapping):
                raise AmapServiceError("高德 REST API 返回值不是 JSON 对象")
            if str(payload.get("status", "1")) == "1":
                return dict(payload)

            info = str(payload.get("info") or "未知错误")
            infocode = str(payload.get("infocode") or "")
            if (
                info in self.QPS_LIMIT_ERRORS
                or infocode in self.QPS_LIMIT_ERRORS
            ) and attempt < self._max_retries:
                logger.warning("触发高德 QPS 限制，准备第 %s 次退避重试", attempt + 1)
                self._retry_delay(attempt)
                continue
            raise AmapServiceError(f"高德 REST API 返回失败状态：{info}")

        raise AmapServiceError("高德 REST API 重试后仍然失败") from last_error

    @staticmethod
    def _merge_pois(
        mcp_pois: list[POIEvidence],
        rest_pois: list[POIEvidence],
    ) -> list[POIEvidence]:
        """优先采用字段更完整的 REST POI，并保留 MCP 独有结果。"""

        merged: dict[str, POIEvidence] = {poi.poi_id: poi for poi in mcp_pois}
        for poi in rest_pois:
            merged[poi.poi_id] = poi
        return list(merged.values())

    def search_verified_pois(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
        limit: int = 10,
    ) -> list[POIEvidence]:
        """搜索 POI，并尽可能补充开放时间等 business 字段。"""

        mcp_pois: list[POIEvidence] = []
        mcp_error: Exception | None = None
        if self._mcp_enabled:
            try:
                payload = self._call_mcp(
                    "maps_text_search",
                    {
                        "keywords": keywords,
                        "city": city,
                        "citylimit": str(citylimit).lower(),
                    },
                )
                mcp_pois = parse_poi_evidence(
                    payload,
                    EvidenceSource(provider="amap", method="mcp.maps_text_search"),
                )
            except Exception as exc:
                mcp_error = exc
                logger.warning("高德 MCP POI 查询失败：%s", type(exc).__name__)

        rest_pois: list[POIEvidence] = []
        rest_error: Exception | None = None
        need_enrichment = not mcp_pois or any(
            not poi.opening_hours.weekly_windows for poi in mcp_pois
        )
        if need_enrichment:
            try:
                payload = self._rest_get(
                    self.POI_SEARCH_URL,
                    {
                        "keywords": keywords,
                        "region": city,
                        "city_limit": str(citylimit).lower(),
                        "show_fields": "business",
                        "page_size": min(max(limit, 1), 25),
                    },
                )
                rest_pois = parse_poi_evidence(
                    payload,
                    EvidenceSource(provider="amap", method="rest.v5.place.text"),
                )
            except Exception as exc:
                rest_error = exc
                logger.warning("高德 REST POI 查询失败：%s", type(exc).__name__)

        pois = self._merge_pois(mcp_pois, rest_pois)[:limit]
        if pois:
            return pois

        reason = rest_error or mcp_error
        raise AmapServiceError("POI 查询没有获得可用的结构化数据") from reason

    def search_poi(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
    ) -> list[POIInfo]:
        """兼容原有地图 API 的 POI 返回模型。"""

        pois = self.search_verified_pois(keywords, city, citylimit)
        return [
            POIInfo(
                id=poi.poi_id,
                name=poi.name,
                type=poi.categories[0] if poi.categories else "未知",
                address=poi.address,
                location=poi.location,
                tel=poi.telephone,
            )
            for poi in pois
        ]

    def get_weather_evidence(self, city: str) -> WeatherEvidence:
        """获取结构化天气预报；MCP 无结果时回退到 REST。"""

        forecasts: list[WeatherInfo] = []
        source = EvidenceSource(provider="amap", method="mcp.maps_weather")
        if self._mcp_enabled:
            try:
                payload = self._call_mcp("maps_weather", {"city": city})
                forecasts = parse_weather_infos(payload)
            except Exception as exc:
                logger.warning("高德 MCP 天气查询失败：%s", type(exc).__name__)

        if not forecasts:
            payload = self._rest_get(
                self.WEATHER_URL,
                {"city": city, "extensions": "all"},
            )
            forecasts = parse_weather_infos(payload)
            source = EvidenceSource(provider="amap", method="rest.v3.weather")

        if not forecasts:
            raise AmapServiceError("天气查询没有获得可用的预报数据")
        return WeatherEvidence(
            city=city,
            forecasts=forecasts,
            status=EvidenceStatus.SUCCESS,
            source=source,
        )

    def get_weather(self, city: str) -> list[WeatherInfo]:
        """兼容原有天气接口。"""

        return self.get_weather_evidence(city).forecasts

    def geocode(self, address: str, city: str | None = None) -> Location:
        """将地址转换为经纬度，MCP 无法解析时回退到 REST。"""

        if self._mcp_enabled:
            try:
                arguments: dict[str, Any] = {"address": address}
                if city:
                    arguments["city"] = city
                payload = self._call_mcp("maps_geo", arguments)
                locations = self._extract_geocode_locations(payload)
                if locations:
                    return locations[0]
            except Exception as exc:
                logger.warning("高德 MCP 地理编码失败：%s", type(exc).__name__)

        params: dict[str, Any] = {"address": address}
        if city:
            params["city"] = city
        payload = self._rest_get(self.GEOCODE_URL, params)
        locations = self._extract_geocode_locations(payload)
        if not locations:
            raise AmapServiceError(f"地址无法解析为坐标：{address}")
        return locations[0]

    @staticmethod
    def _extract_geocode_locations(payload: Any) -> list[Location]:
        data = extract_json_payload(payload)
        if not isinstance(data, Mapping):
            return []
        records = data.get("geocodes")
        if not isinstance(records, list):
            records = [data]
        locations: list[Location] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            location = parse_location(record.get("location", record))
            if location:
                locations.append(location)
        return locations

    def get_route(
        self,
        *,
        origin_id: str,
        destination_id: str,
        origin: Location,
        destination: Location,
        mode: TravelMode,
        city: str | None = None,
    ) -> RouteEvidence:
        """通过高德 REST 查询两点之间的路线证据。"""

        cache_key = (
            origin_id,
            destination_id,
            mode.value,
            city or "",
            origin.longitude,
            origin.latitude,
            destination.longitude,
            destination.latitude,
        )
        with self._route_cache_lock:
            cached = self._route_cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(deep=True)

        params: dict[str, Any] = {
            "origin": f"{origin.longitude},{origin.latitude}",
            "destination": f"{destination.longitude},{destination.latitude}",
        }
        if mode == TravelMode.TRANSIT:
            if not city:
                raise AmapServiceError("公交路线查询必须提供城市")
            params["city"] = city

        payload = self._rest_get(self.ROUTE_URLS[mode], params)
        route = parse_route_evidence(
            payload,
            origin_id=origin_id,
            destination_id=destination_id,
            mode=mode,
            source=EvidenceSource(
                provider="amap",
                method=f"rest.v3.direction.{mode.value}",
            ),
        )
        if route is None and mode == TravelMode.TRANSIT:
            # 很近的两个景点可能没有公交换乘方案。公共交通出行仍允许短距离
            # 步行衔接，此时使用真实步行路线，而不是编造公交耗时。
            walking_payload = self._rest_get(
                self.ROUTE_URLS[TravelMode.WALKING],
                {
                    "origin": params["origin"],
                    "destination": params["destination"],
                },
            )
            route = parse_route_evidence(
                walking_payload,
                origin_id=origin_id,
                destination_id=destination_id,
                mode=mode,
                source=EvidenceSource(
                    provider="amap",
                    method="rest.v3.direction.walking_fallback_for_transit",
                ),
            )
            if route is not None:
                route.description = (
                    "公交无可用换乘方案，采用步行衔接；" + route.description
                ).rstrip("；")
        if route is None:
            raise AmapServiceError("路线响应缺少距离或耗时字段")
        with self._route_cache_lock:
            self._route_cache[cache_key] = route
        return route

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: str | None = None,
        destination_city: str | None = None,
        route_type: str = "walking",
    ) -> dict[str, Any]:
        """兼容原有地图 API，并返回 RouteInfo 所需字段。"""

        try:
            mode = TravelMode(route_type)
        except ValueError:
            mode = TravelMode.WALKING
        origin = self.geocode(origin_address, origin_city)
        destination = self.geocode(destination_address, destination_city)
        route = self.get_route(
            origin_id=origin_address,
            destination_id=destination_address,
            origin=origin,
            destination=destination,
            mode=mode,
            city=origin_city or destination_city,
        )
        return {
            "distance": float(route.distance_meters),
            "duration": route.duration_seconds,
            "route_type": route.mode.value,
            "description": route.description or "高德地图路线规划结果",
        }

    def get_poi_detail(self, poi_id: str) -> dict[str, Any]:
        """查询 POI 详情，并返回可追溯的原始结构化数据。"""

        if self._mcp_enabled:
            try:
                payload = self._call_mcp("maps_search_detail", {"id": poi_id})
                if isinstance(payload, Mapping):
                    return dict(payload)
            except Exception as exc:
                logger.warning("高德 MCP POI 详情失败：%s", type(exc).__name__)

        return self._rest_get(
            self.POI_DETAIL_URL,
            {
                "id": poi_id,
                "show_fields": "business",
            },
        )


_amap_service: AmapService | None = None


def get_amap_service() -> AmapService:
    """获取进程内复用的高德结构化数据服务。"""

    global _amap_service
    if _amap_service is None:
        _amap_service = AmapService()
    return _amap_service
