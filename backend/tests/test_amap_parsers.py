"""高德结构化响应解析测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.models.planning import (
    EvidenceSource,
    PriceStatus,
    TravelMode,
    VerificationStatus,
)
from app.services.amap_parsers import (
    extract_json_payload,
    parse_poi_evidence,
    parse_route_evidence,
    parse_weather_infos,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_extracts_json_from_mcp_text_content() -> None:
    payload = load_fixture("amap_poi_search.json")
    wrapped = {
        "content": [
            {
                "type": "text",
                "text": f"工具查询成功：\n{json.dumps(payload, ensure_ascii=False)}",
            }
        ]
    }

    extracted = extract_json_payload(wrapped)

    assert extracted["pois"][0]["id"] == "B0TEST001"


def test_poi_parser_preserves_opening_evidence_and_unknown_ticket_price() -> None:
    payload = load_fixture("amap_poi_search.json")

    pois = parse_poi_evidence(
        payload,
        EvidenceSource(provider="amap", method="fixture"),
    )

    assert len(pois) == 1
    poi = pois[0]
    assert poi.location.longitude == 121.4737
    assert poi.opening_hours.status == VerificationStatus.VERIFIED
    assert poi.opening_hours.weekly_windows[0][0].start == "09:00"
    assert poi.opening_hours.weekly_windows[6][0].end == "16:00"
    assert poi.opening_hours.holiday_exception is True
    assert poi.ticket_price.status == PriceStatus.UNKNOWN
    assert poi.ticket_price.amount_yuan is None
    assert poi.ticket_price.raw_text == "88"


def test_route_parser_returns_real_duration_and_distance() -> None:
    payload = load_fixture("amap_route_walking.json")

    route = parse_route_evidence(
        payload,
        origin_id="A",
        destination_id="B",
        mode=TravelMode.WALKING,
        source=EvidenceSource(provider="amap", method="fixture"),
    )

    assert route is not None
    assert route.distance_meters == 1850
    assert route.duration_seconds == 1560
    assert route.duration_minutes == 26
    assert "右转进入测试路" in route.description


def test_weather_parser_reads_forecast_casts() -> None:
    forecasts = parse_weather_infos(load_fixture("amap_weather.json"))

    assert len(forecasts) == 1
    assert forecasts[0].date == "2026-11-10"
    assert forecasts[0].day_temp == 22


def test_weather_parser_uses_first_match_when_city_and_county_are_returned() -> None:
    cast = {
        "date": "2026-11-05",
        "dayweather": "多云",
        "nightweather": "晴",
        "daytemp": "21",
        "nighttemp": "5",
    }
    payload = {
        "status": "1",
        "forecasts": [
            {"city": "安阳市", "casts": [cast]},
            {"city": "安阳县", "casts": [{**cast, "daytemp": "20"}]},
        ],
    }

    forecasts = parse_weather_infos(payload)

    assert len(forecasts) == 1
    assert forecasts[0].day_temp == 21
