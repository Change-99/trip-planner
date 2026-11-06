"""高德 MCP 与 REST 响应的确定性解析函数。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.models.planning import (
    EvidenceSource,
    OpeningHoursEvidence,
    POIEvidence,
    PriceEvidence,
    PriceStatus,
    RouteEvidence,
    TimeWindow,
    TravelMode,
    VerificationStatus,
)
from app.models.schemas import Location, WeatherInfo

_WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def extract_json_payload(value: Any) -> Any:
    """从 MCP 文本包装或普通响应中提取 JSON 数据。"""

    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parsed = extract_json_payload(item["text"])
                    if parsed is not None:
                        return parsed
        return dict(value)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)

    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        if isinstance(parsed, Mapping) and "content" in parsed:
            return extract_json_payload(parsed)
        return parsed

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return extract_json_payload(parsed)
    return None


def parse_location(value: Any) -> Location | None:
    """解析高德常见的经纬度格式。"""

    if isinstance(value, Mapping):
        longitude = value.get("longitude", value.get("lng", value.get("lon")))
        latitude = value.get("latitude", value.get("lat"))
        try:
            return Location(longitude=float(longitude), latitude=float(latitude))
        except (TypeError, ValueError):
            return None

    if isinstance(value, str) and "," in value:
        longitude, latitude = value.split(",", 1)
        try:
            return Location(longitude=float(longitude), latitude=float(latitude))
        except ValueError:
            return None
    return None


def _expand_weekdays(label: str) -> list[int]:
    if label in {"每天", "每日"}:
        return list(range(7))

    days = re.findall(r"周([一二三四五六日天])", label)
    if not days:
        return []
    start = _WEEKDAY_MAP[days[0]]
    if len(days) == 1:
        return [start]
    end = _WEEKDAY_MAP[days[1]]
    if end >= start:
        return list(range(start, end + 1))
    return list(range(start, 7)) + list(range(0, end + 1))


def _parse_time_windows(text: str) -> list[TimeWindow]:
    if re.search(r"24\s*小时|全天", text):
        return [TimeWindow(start="00:00", end="23:59")]

    windows: list[TimeWindow] = []
    pattern = re.compile(
        r"(?<!\d)(\d{1,2})[:：](\d{2})\s*[-—~至]\s*"
        r"(\d{1,2})[:：](\d{2})(?!\d)"
    )
    for start_hour, start_minute, end_hour, end_minute in pattern.findall(text):
        start = f"{int(start_hour):02d}:{int(start_minute):02d}"
        end = f"{int(end_hour):02d}:{int(end_minute):02d}"
        window = TimeWindow(start=start, end=end)
        if window not in windows:
            windows.append(window)
    return windows


def parse_opening_hours(raw_text: Any) -> OpeningHoursEvidence:
    """把高德的每周营业时间文本转换为星期到时间窗口的映射。"""

    if not isinstance(raw_text, str) or not raw_text.strip():
        return OpeningHoursEvidence(notes=["数据源未返回开放时间"])

    text = raw_text.strip().replace("；", ";")
    holiday_exception = bool(re.search(r"节假日|法定假日", text))
    weekly_windows: dict[int, list[TimeWindow]] = {}

    label_pattern = re.compile(
        r"((?:周[一二三四五六日天])(?:\s*(?:至|到|[-—~])\s*周?[一二三四五六日天])?|每天|每日)"
        r"\s*[:：]\s*"
    )
    matches = list(label_pattern.finditer(text))
    if matches:
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[match.end():segment_end]
            windows = _parse_time_windows(segment)
            for weekday in _expand_weekdays(match.group(1)):
                weekly_windows.setdefault(weekday, [])
                for window in windows:
                    if window not in weekly_windows[weekday]:
                        weekly_windows[weekday].append(window)
    else:
        windows = _parse_time_windows(text)
        if windows:
            weekly_windows = {weekday: list(windows) for weekday in range(7)}

    notes: list[str] = []
    if holiday_exception:
        notes.append("原始开放时间包含节假日例外，具体旅行日期需要再次核实")
    status = (
        VerificationStatus.VERIFIED
        if weekly_windows
        else VerificationStatus.UNKNOWN
    )
    if not weekly_windows:
        notes.append("无法从原始文本解析出开放时间窗口")

    return OpeningHoursEvidence(
        status=status,
        weekly_windows=weekly_windows,
        raw_text=raw_text,
        holiday_exception=holiday_exception,
        notes=notes,
    )


def _find_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
        for value in payload.values():
            found = _find_list(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        if all(isinstance(item, Mapping) for item in payload):
            return [dict(item) for item in payload]
        for item in payload:
            found = _find_list(item, keys)
            if found:
                return found
    return []


def parse_poi_evidence(payload: Any, source: EvidenceSource) -> list[POIEvidence]:
    """解析 POI 列表，并明确把门票价格保留为未知。"""

    data = extract_json_payload(payload)
    records = _find_list(data, ("pois", "results", "items"))
    pois: list[POIEvidence] = []

    for record in records:
        location = parse_location(record.get("location"))
        poi_id = str(record.get("id") or record.get("poi_id") or "").strip()
        name = str(record.get("name") or "").strip()
        if not location or not poi_id or not name:
            continue

        business = record.get("business")
        if not isinstance(business, Mapping):
            business = record.get("biz_ext")
        if not isinstance(business, Mapping):
            business = {}
        opening_raw = (
            business.get("opentime_week")
            or business.get("open_time")
            or record.get("opentime_week")
            or record.get("open_time")
        )

        rating_raw = business.get("rating", record.get("rating"))
        try:
            rating = float(rating_raw) if rating_raw not in (None, "") else None
        except (TypeError, ValueError):
            rating = None

        category_raw = record.get("type") or record.get("type_name") or ""
        categories = [item.strip() for item in str(category_raw).split(";") if item.strip()]
        telephone = business.get("tel") or record.get("tel")

        # 高德 business.cost 表示人均消费，不等同于景点门票。
        ticket_price = PriceEvidence(
            status=PriceStatus.UNKNOWN,
            raw_text=str(business.get("cost")) if business.get("cost") else None,
            notes=["高德 cost 字段不作为已核实门票价格"],
        )
        pois.append(
            POIEvidence(
                poi_id=poi_id,
                name=name,
                address=str(record.get("address") or ""),
                location=location,
                categories=categories,
                telephone=str(telephone) if telephone else None,
                rating=rating,
                opening_hours=parse_opening_hours(opening_raw),
                ticket_price=ticket_price,
                source=source,
                raw_data=record,
            )
        )
    return pois


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_route_evidence(
    payload: Any,
    *,
    origin_id: str,
    destination_id: str,
    mode: TravelMode,
    source: EvidenceSource,
) -> RouteEvidence | None:
    """解析高德步行、驾车或公交路径结果。"""

    data = extract_json_payload(payload)
    if not isinstance(data, Mapping):
        return None
    route = data.get("route", data)
    if not isinstance(route, Mapping):
        return None

    candidates = route.get("paths") or route.get("transits") or data.get("paths")
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None

    duration = _as_int(candidate.get("duration"))
    distance = _as_int(candidate.get("distance", route.get("distance")))
    if duration is None or distance is None:
        return None

    cost_raw = candidate.get("cost") or candidate.get("tolls")
    try:
        cost = float(cost_raw) if cost_raw not in (None, "") else None
    except (TypeError, ValueError):
        cost = None
    estimated_cost = PriceEvidence(
        status=PriceStatus.ESTIMATED_RANGE if cost is not None else PriceStatus.UNKNOWN,
        amount_yuan=cost,
        raw_text=str(cost_raw) if cost_raw not in (None, "") else None,
        notes=["路线费用为数据源估算值，实际费用可能变化"] if cost is not None else [],
    )

    descriptions: list[str] = []
    steps = candidate.get("steps") or candidate.get("segments") or []
    if isinstance(steps, list):
        for step in steps[:3]:
            if isinstance(step, Mapping) and step.get("instruction"):
                descriptions.append(str(step["instruction"]))

    return RouteEvidence(
        origin_id=origin_id,
        destination_id=destination_id,
        mode=mode,
        distance_meters=distance,
        duration_seconds=duration,
        estimated_cost=estimated_cost,
        description="；".join(descriptions),
        source=source,
    )


def parse_weather_infos(payload: Any) -> list[WeatherInfo]:
    """解析高德天气预报。"""

    data = extract_json_payload(payload)
    records = _find_list(data, ("casts", "forecasts"))
    # 城市名称可能同时命中市区和同名县，高德会返回多组 forecasts。
    # 搜索结果按相关性排序，使用第一组，避免把外层城市对象误当作单日预报。
    if records and isinstance(records[0].get("casts"), list):
        records = [dict(item) for item in records[0]["casts"] if isinstance(item, Mapping)]

    forecasts: list[WeatherInfo] = []
    for record in records:
        forecast_date = record.get("date")
        if not forecast_date:
            continue
        forecasts.append(
            WeatherInfo(
                date=str(forecast_date),
                day_weather=str(record.get("dayweather", record.get("day_weather", ""))),
                night_weather=str(record.get("nightweather", record.get("night_weather", ""))),
                day_temp=record.get("daytemp", record.get("day_temp", 0)),
                night_temp=record.get("nighttemp", record.get("night_temp", 0)),
                wind_direction=str(record.get("daywind", record.get("wind_direction", ""))),
                wind_power=str(record.get("daypower", record.get("wind_power", ""))),
            )
        )
    return forecasts
