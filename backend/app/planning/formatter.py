"""把约束求解结果转换为现有 FastAPI 响应模型。"""

from __future__ import annotations

from app.models.planning import (
    BudgetAssessment,
    EvidenceBundle,
    GroundedAdvice,
    OptimizedItinerary,
    PlanningConstraints,
    PriceCategory,
    PriceEvidence,
    PriceStatus,
)
from app.models.schemas import (
    Attraction,
    Budget,
    BudgetComponentView,
    DayPlan,
    Hotel,
    Meal,
    TravelAdvice,
    TripPlan,
)


def format_trip_plan(
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    itinerary: OptimizedItinerary,
    budget_assessment: BudgetAssessment | None = None,
    advice: GroundedAdvice | None = None,
) -> TripPlan:
    """保持旧前端字段兼容，同时附加时间轴、限制和评分。"""

    poi_by_id = {poi.poi_id: poi for poi in evidence.pois}
    hotel = _selected_hotel(evidence)
    days: list[DayPlan] = []
    for day_index, scheduled_day in enumerate(itinerary.days):
        attractions: list[Attraction] = []
        meals: list[Meal] = []
        timeline = []
        for activity in scheduled_day.activities:
            timeline.append(
                {
                    "type": activity.activity_type,
                    "name": activity.name,
                    "start_time": _clock(activity.start_minute),
                    "end_time": _clock(activity.end_minute),
                    "poi_id": activity.poi_id,
                }
            )
            if activity.activity_type == "attraction" and activity.poi_id:
                poi = poi_by_id[activity.poi_id]
                verified_price = poi.ticket_price.status in {
                    PriceStatus.VERIFIED,
                    PriceStatus.FREE,
                }
                attractions.append(
                    Attraction(
                        name=poi.name,
                        address=poi.address,
                        location=poi.location,
                        visit_duration=activity.end_minute - activity.start_minute,
                        description=(
                            f"计划参观 {_clock(activity.start_minute)}-"
                            f"{_clock(activity.end_minute)}；开放时间来源：{poi.source.method}"
                        ),
                        category=poi.categories[0] if poi.categories else "景点",
                        rating=poi.rating,
                        poi_id=poi.poi_id,
                        ticket_price=int(poi.ticket_price.amount_yuan or 0)
                        if verified_price
                        else None,
                    )
                )
            elif activity.activity_type == "meal":
                meal_type = "lunch" if "午餐" in activity.name else "dinner"
                meals.append(
                    Meal(
                        type=meal_type,
                        name=activity.name,
                        description=(
                            f"已预留 {_clock(activity.start_minute)}-"
                            f"{_clock(activity.end_minute)}，具体餐厅未核实"
                        ),
                    )
                )

        days.append(
            DayPlan(
                date=scheduled_day.date.isoformat(),
                day_index=day_index,
                description="；".join(
                    f"{item['start_time']} {item['name']}" for item in timeline
                ),
                transportation=constraints.transportation.value,
                accommodation=constraints.accommodation,
                hotel=hotel,
                attractions=attractions,
                meals=meals,
                timeline=timeline,
            )
        )

    weather = evidence.weather.forecasts if evidence.weather else []
    limitations = list(
        dict.fromkeys(
            [
                *evidence.limitations,
                *itinerary.limitations,
                *(budget_assessment.notes if budget_assessment else []),
            ]
        )
    )
    planning_summary = (
        advice.planning_summary
        if advice
        else "行程已由约束求解器生成，并通过开放时间、活动冲突、用餐预留和交通衔接校验。"
    )
    budget = _budget_view(budget_assessment)
    return TripPlan(
        city=constraints.city,
        start_date=constraints.start_date.isoformat(),
        end_date=constraints.end_date.isoformat(),
        days=days,
        weather_info=weather,
        overall_suggestions=planning_summary,
        planning_summary=planning_summary,
        travel_advice=[
            TravelAdvice.model_validate(item.model_dump())
            for item in (advice.items if advice else [])
        ],
        budget=budget,
        planning_status=itinerary.solver_status,
        limitations=limitations,
        score_breakdown=itinerary.score.model_dump(),
    )


def _selected_hotel(evidence: EvidenceBundle) -> Hotel | None:
    if not evidence.hotels:
        return None
    hotel_quote = next(
        (
            quote
            for quote in evidence.price_quotes
            if quote.category == PriceCategory.HOTEL_NIGHT and quote.entity_id
        ),
        None,
    )
    hotel = next(
        (
            item
            for item in evidence.hotels
            if hotel_quote and item.poi_id == hotel_quote.entity_id
        ),
        evidence.hotels[0],
    )
    reference_price = hotel.reference_price
    return Hotel(
        name=hotel.name,
        address=hotel.address,
        location=hotel.location,
        rating=str(hotel.rating or ""),
        type=hotel.categories[0] if hotel.categories else "酒店",
        price_range=_price_label(reference_price),
        estimated_cost=(
            int(reference_price.amount_yuan)
            if reference_price.status == PriceStatus.VERIFIED
            and reference_price.amount_yuan is not None
            else None
        ),
    )


def _budget_view(assessment: BudgetAssessment | None) -> Budget | None:
    if assessment is None:
        return None
    return Budget(
        status=assessment.status,
        confirmed_total_yuan=assessment.confirmed_total_yuan,
        estimated_minimum_yuan=assessment.estimated_minimum_yuan,
        estimated_maximum_yuan=assessment.estimated_maximum_yuan,
        components=[
            BudgetComponentView.model_validate(item.model_dump(mode="json"))
            for item in assessment.components
        ],
        unknown_items=assessment.unknown_items,
        notes=assessment.notes,
    )


def _price_label(price: PriceEvidence) -> str:
    if price.status == PriceStatus.FREE:
        return "免费"
    if price.status == PriceStatus.VERIFIED and price.amount_yuan is not None:
        return f"已核实 ¥{price.amount_yuan:g}"
    if price.status == PriceStatus.ESTIMATED_RANGE:
        low = price.minimum_yuan if price.minimum_yuan is not None else price.amount_yuan
        high = price.maximum_yuan if price.maximum_yuan is not None else price.amount_yuan
        if low is not None and high is not None:
            return f"参考 ¥{low:g}–{high:g}"
    return "价格未核实"


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"
