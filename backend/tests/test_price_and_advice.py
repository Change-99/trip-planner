"""价格 MCP 解析、部分预算和建议事实约束测试。"""

from datetime import date

from app.models.planning import (
    AdviceDraft,
    AdviceFact,
    AdviceItem,
    BudgetAssessment,
    EvidenceBundle,
    EvidenceSource,
    OptimizedItinerary,
    PlanningConstraints,
    PriceCategory,
    PriceEvidence,
    PriceQuoteEvidence,
    PriceStatus,
    ScheduledActivity,
    ScheduledDay,
    TravelMode,
)
from app.planning.advisor import ground_advice
from app.planning.budget import calculate_budget
from app.services.price_mcp_service import (
    InMemoryPriceMCPTool,
    PriceMCPService,
    parse_price_evidence,
)
from test_trip_graph import make_poi


def test_trusted_ticket_source_can_produce_verified_exact_price() -> None:
    price = parse_price_evidence(
        "成人票价格80元。",
        category=PriceCategory.TICKET,
        source_url="https://museum.gov.cn/ticket",
        source_title="票务公告",
        trusted_domains=set(),
        applicable_date=date(2026, 11, 10),
    )

    assert price.status == PriceStatus.VERIFIED
    assert price.amount_yuan == 80
    assert price.source_url == "https://museum.gov.cn/ticket"


def test_untrusted_exact_price_is_only_an_estimate() -> None:
    price = parse_price_evidence(
        "酒店每晚价格399元。",
        category=PriceCategory.HOTEL_NIGHT,
        source_url="https://booking.example/hotel",
        source_title="预订页",
        trusted_domains=set(),
    )

    assert price.status == PriceStatus.ESTIMATED_RANGE
    assert price.amount_yuan is None
    assert price.minimum_yuan == 399
    assert price.maximum_yuan == 399


def test_price_service_calls_search_and_extract_mcp_tools() -> None:
    class FakeMCPTool:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, request):
            tool_name = request["tool_name"]
            self.calls.append(tool_name)
            if tool_name == "search_ticket_price":
                return {
                    "results": [
                        {
                            "title": "博物馆票务公告",
                            "url": "https://museum.example/ticket",
                            "content": "票务信息",
                        }
                    ]
                }
            if tool_name == "extract_price_source":
                return {
                    "url": "https://museum.example/ticket",
                    "content": "成人票价格80元。",
                }
            raise AssertionError(tool_name)

    tool = FakeMCPTool()
    service = PriceMCPService(
        mcp_tool=tool,
        trusted_domains={"museum.example"},
    )

    quote = service.ticket_quote(
        quote_id="ticket:p1",
        entity_id="p1",
        entity_name="历史博物馆",
        city="上海",
        visit_start=date(2026, 11, 10),
        visit_end=date(2026, 11, 10),
    )

    assert quote.price.status == PriceStatus.VERIFIED
    assert quote.price.amount_yuan == 80
    assert tool.calls == ["search_ticket_price", "extract_price_source"]


def test_in_memory_price_mcp_tool_does_not_need_stdio_subprocess() -> None:
    from fastmcp import FastMCP

    server = FastMCP("price-test")

    @server.tool
    def fixed_quote(entity_name: str) -> dict:
        return {"entity_name": entity_name, "amount": 80}

    result = InMemoryPriceMCPTool(server).run(
        {
            "tool_name": "fixed_quote",
            "arguments": {"entity_name": "测试博物馆"},
        }
    )

    assert result == {"entity_name": "测试博物馆", "amount": 80}


def test_generic_meal_price_from_one_source_is_not_used_as_budget() -> None:
    class FakeMCPTool:
        def run(self, request):
            assert request["tool_name"] == "search_meal_price"
            return {
                "results": [
                    {
                        "title": "某高端餐厅报道",
                        "url": "https://news.example/restaurant",
                        "content": "该餐厅人均消费1000元。",
                    }
                ]
            }

    service = PriceMCPService(mcp_tool=FakeMCPTool(), trusted_domains=set())
    quote = service.meal_quote(
        quote_id="meal:上海",
        city="上海",
        dining_preference="当地餐饮",
    )

    assert quote.price.status == PriceStatus.UNKNOWN
    assert quote.price.minimum_yuan is None
    assert "单一来源不采信" in quote.price.notes[-1]


def test_grounder_rejects_unsupported_number_and_keeps_grounded_advice() -> None:
    draft = AdviceDraft(
        planning_summary="已通过校验",
        facts=[AdviceFact(fact_id="weather", text="当天有雨", source_type="weather")],
        items=[
            AdviceItem(
                category="天气",
                content="建议携带雨具。",
                evidence_refs=["weather"],
            ),
            AdviceItem(
                category="预算",
                content="门票需要100元。",
                evidence_refs=["weather"],
            ),
        ],
    )

    grounded = ground_advice(draft)

    assert [item.content for item in grounded.items] == ["建议携带雨具。"]
    assert grounded.rejected_count == 1


def test_budget_uses_verified_and_range_prices_without_zero_filling_unknowns() -> None:
    source = EvidenceSource(provider="fixture", method="price")
    poi = make_poi("p1", "历史博物馆", 121.47)
    poi.ticket_price = PriceEvidence(
        status=PriceStatus.VERIFIED,
        amount_yuan=50,
        source_url="https://museum.gov.cn/ticket",
        source=source,
    )
    meal_quote = PriceQuoteEvidence(
        quote_id="meal:上海",
        category=PriceCategory.MEAL_PERSON,
        entity_name="当地餐饮",
        price=PriceEvidence(
            status=PriceStatus.ESTIMATED_RANGE,
            minimum_yuan=30,
            maximum_yuan=50,
            source_url="https://example.com/meal",
            source=source,
        ),
        source=source,
    )
    itinerary = OptimizedItinerary(
        city="上海",
        solver_status="feasible",
        selected_poi_ids=["p1"],
        days=[
            ScheduledDay(
                date=date(2026, 11, 10),
                activities=[
                    ScheduledActivity(
                        activity_type="attraction",
                        poi_id="p1",
                        name=poi.name,
                        start_minute=540,
                        end_minute=630,
                    ),
                    ScheduledActivity(
                        activity_type="meal",
                        name="午餐",
                        start_minute=720,
                        end_minute=780,
                    ),
                ],
            )
        ],
    )
    constraints = PlanningConstraints(
        city="上海",
        start_date=date(2026, 11, 10),
        end_date=date(2026, 11, 10),
        travel_days=1,
        transportation=TravelMode.TRANSIT,
        accommodation="经济型酒店",
        traveler_count=2,
    )

    budget = calculate_budget(
        constraints,
        EvidenceBundle(pois=[poi], price_quotes=[meal_quote]),
        itinerary,
    )

    assert budget.status == "partial"
    assert budget.confirmed_total_yuan == 100
    assert budget.estimated_minimum_yuan == 160
    assert budget.estimated_maximum_yuan == 200
    assert "交通费用" in budget.unknown_items
