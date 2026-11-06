"""高德结构化服务的离线测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.models.planning import TravelMode, VerificationStatus
from app.models.schemas import Location
from app.services.amap_service import AmapService

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class FakeMCPTool:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def run(self, request):
        self.calls.append(request)
        return json.dumps(self.payload, ensure_ascii=False)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, params))
        return FakeResponse(self.responses[url])


class SequencedHttpClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, params))
        return FakeResponse(self.payloads.pop(0))


def test_search_enriches_incomplete_mcp_poi_with_rest_business_fields() -> None:
    mcp_payload = {
        "pois": [
            {
                "id": "B0TEST001",
                "name": "测试博物馆",
                "address": "上海市测试路1号",
                "location": "121.473700,31.230400",
            }
        ]
    }
    http = FakeHttpClient(
        {AmapService.POI_SEARCH_URL: load_fixture("amap_poi_search.json")}
    )
    service = AmapService(
        mcp_tool=FakeMCPTool(mcp_payload),
        http_client=http,
        api_key="test-key",
    )

    pois = service.search_verified_pois("博物馆", "上海")

    assert len(pois) == 1
    assert pois[0].source.method == "rest.v5.place.text"
    assert pois[0].opening_hours.status == VerificationStatus.VERIFIED
    assert http.calls[0][1]["show_fields"] == "business"


def test_get_route_returns_typed_evidence() -> None:
    http = FakeHttpClient(
        {AmapService.ROUTE_URLS[TravelMode.WALKING]: load_fixture("amap_route_walking.json")}
    )
    service = AmapService(
        mcp_tool=FakeMCPTool({}),
        http_client=http,
        api_key="test-key",
    )

    route = service.get_route(
        origin_id="A",
        destination_id="B",
        origin=Location(longitude=121.47, latitude=31.23),
        destination=Location(longitude=121.48, latitude=31.24),
        mode=TravelMode.WALKING,
    )

    assert route.duration_seconds == 1560
    assert route.source.method == "rest.v3.direction.walking"
    assert http.calls[0][1]["key"] == "test-key"


def test_weather_falls_back_to_rest_when_mcp_has_no_forecast() -> None:
    http = FakeHttpClient(
        {AmapService.WEATHER_URL: load_fixture("amap_weather.json")}
    )
    service = AmapService(
        mcp_tool=FakeMCPTool({"status": "1"}),
        http_client=http,
        api_key="test-key",
    )

    evidence = service.get_weather_evidence("上海")

    assert evidence.forecasts[0].date == "2026-11-10"
    assert evidence.source.method == "rest.v3.weather"


def test_rest_retries_after_qps_limit() -> None:
    success = load_fixture("amap_route_walking.json")
    http = SequencedHttpClient(
        [
            {
                "status": "0",
                "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                "infocode": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
            },
            success,
        ]
    )
    service = AmapService(
        mcp_tool=FakeMCPTool({}),
        http_client=http,
        api_key="test-key",
        max_qps=0,
        retry_backoff_seconds=0,
    )

    route = service.get_route(
        origin_id="A",
        destination_id="B",
        origin=Location(longitude=121.47, latitude=31.23),
        destination=Location(longitude=121.48, latitude=31.24),
        mode=TravelMode.WALKING,
    )

    assert route.duration_seconds == 1560
    assert len(http.calls) == 2


def test_route_cache_avoids_duplicate_rest_request() -> None:
    http = FakeHttpClient(
        {AmapService.ROUTE_URLS[TravelMode.WALKING]: load_fixture("amap_route_walking.json")}
    )
    service = AmapService(
        mcp_tool=FakeMCPTool({}),
        http_client=http,
        api_key="test-key",
        max_qps=0,
    )
    arguments = {
        "origin_id": "A",
        "destination_id": "B",
        "origin": Location(longitude=121.47, latitude=31.23),
        "destination": Location(longitude=121.48, latitude=31.24),
        "mode": TravelMode.WALKING,
    }

    first = service.get_route(**arguments)
    second = service.get_route(**arguments)

    assert first.duration_seconds == second.duration_seconds
    assert len(http.calls) == 1


def test_transit_without_plan_falls_back_to_verified_walking_route() -> None:
    http = FakeHttpClient(
        {
            AmapService.ROUTE_URLS[TravelMode.TRANSIT]: {
                "status": "1",
                "route": {"transits": []},
            },
            AmapService.ROUTE_URLS[TravelMode.WALKING]: load_fixture(
                "amap_route_walking.json"
            ),
        }
    )
    service = AmapService(
        mcp_tool=FakeMCPTool({}),
        http_client=http,
        api_key="test-key",
        max_qps=0,
    )

    route = service.get_route(
        origin_id="A",
        destination_id="B",
        origin=Location(longitude=121.47, latitude=31.23),
        destination=Location(longitude=121.48, latitude=31.24),
        mode=TravelMode.TRANSIT,
        city="上海",
    )

    assert route.mode == TravelMode.TRANSIT
    assert route.source.method == "rest.v3.direction.walking_fallback_for_transit"
    assert "步行衔接" in route.description
    assert len(http.calls) == 2
