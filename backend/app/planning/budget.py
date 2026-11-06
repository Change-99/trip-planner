"""根据工具证据计算部分预算，不允许把未知金额当作 0。"""

from __future__ import annotations

from app.models.planning import (
    BudgetAssessment,
    BudgetComponent,
    EvidenceBundle,
    OptimizedItinerary,
    PlanningConstraints,
    PriceCategory,
    PriceEvidence,
    PriceStatus,
)


def calculate_budget(
    constraints: PlanningConstraints,
    evidence: EvidenceBundle,
    itinerary: OptimizedItinerary,
) -> BudgetAssessment:
    """按人数、间夜和行程段汇总已核实价格与估算区间。"""

    components: list[BudgetComponent] = []
    unknown: list[str] = []
    poi_by_id = {poi.poi_id: poi for poi in evidence.pois}

    for poi_id in itinerary.selected_poi_ids:
        poi = poi_by_id.get(poi_id)
        if not poi:
            continue
        component = _component(
            PriceCategory.TICKET,
            f"{poi.name}门票 × {constraints.traveler_count}人",
            poi.ticket_price,
            multiplier=constraints.traveler_count,
        )
        if component:
            components.append(component)
        else:
            unknown.append(f"{poi.name}门票")

    nights = max(0, constraints.travel_days - 1)
    hotel_quote = next(
        (
            quote
            for quote in evidence.price_quotes
            if quote.category == PriceCategory.HOTEL_NIGHT
        ),
        None,
    )
    if nights:
        if hotel_quote:
            component = _component(
                PriceCategory.HOTEL_NIGHT,
                f"{hotel_quote.entity_name} × {nights}晚 × {constraints.room_count}间",
                hotel_quote.price,
                multiplier=nights * constraints.room_count,
            )
            if component:
                components.append(component)
            else:
                unknown.append("住宿费用")
        else:
            unknown.append("住宿费用")

    meal_quote = next(
        (
            quote
            for quote in evidence.price_quotes
            if quote.category == PriceCategory.MEAL_PERSON
        ),
        None,
    )
    meal_count = sum(
        activity.activity_type == "meal"
        for day in itinerary.days
        for activity in day.activities
    )
    if meal_count:
        if meal_quote:
            component = _component(
                PriceCategory.MEAL_PERSON,
                f"餐饮参考 × {meal_count}餐 × {constraints.traveler_count}人",
                meal_quote.price,
                multiplier=meal_count * constraints.traveler_count,
            )
            if component:
                components.append(component)
            else:
                unknown.append("餐饮费用")
        else:
            unknown.append("餐饮费用")

    transport_prices = [
        activity.price
        for day in itinerary.days
        for activity in day.activities
        if activity.activity_type == "transport"
    ]
    if transport_prices:
        known_transport = [price for price in transport_prices if _bounds(price)]
        if known_transport:
            combined = _combine_prices(known_transport)
            component = _component(
                PriceCategory.TRANSPORT,
                "行程内交通",
                combined,
                multiplier=1,
            )
            if component:
                components.append(component)
        if len(known_transport) != len(transport_prices):
            unknown.append("部分交通费用")
    else:
        unknown.append("交通费用")

    confirmed_values = [
        item.confirmed_yuan
        for item in components
        if item.confirmed_yuan is not None
    ]
    minimum_values = [
        item.minimum_yuan for item in components if item.minimum_yuan is not None
    ]
    maximum_values = [
        item.maximum_yuan for item in components if item.maximum_yuan is not None
    ]
    all_definite = bool(components) and not unknown and all(
        item.status in {PriceStatus.VERIFIED, PriceStatus.FREE}
        for item in components
    )
    status = "verified" if all_definite else "partial" if components else "unavailable"
    notes = []
    if unknown:
        notes.append("预算不完整：未知项目未按 0 元计入")
    if any(item.status == PriceStatus.ESTIMATED_RANGE for item in components):
        notes.append("估算区间来自实时检索，实际价格可能因日期、票种或房型变化")
    return BudgetAssessment(
        status=status,
        confirmed_total_yuan=sum(confirmed_values) if confirmed_values else None,
        estimated_minimum_yuan=sum(minimum_values) if minimum_values else None,
        estimated_maximum_yuan=sum(maximum_values) if maximum_values else None,
        components=components,
        unknown_items=list(dict.fromkeys(unknown)),
        notes=notes,
    )


def _component(
    category: PriceCategory,
    label: str,
    price: PriceEvidence,
    *,
    multiplier: int,
) -> BudgetComponent | None:
    bounds = _bounds(price)
    if bounds is None:
        return None
    confirmed, minimum, maximum = bounds
    source_urls = [price.source_url] if price.source_url else []
    return BudgetComponent(
        category=category,
        label=label,
        status=price.status,
        confirmed_yuan=confirmed * multiplier if confirmed is not None else None,
        minimum_yuan=minimum * multiplier,
        maximum_yuan=maximum * multiplier,
        source_urls=source_urls,
        notes=price.notes,
    )


def _bounds(price: PriceEvidence) -> tuple[float | None, float, float] | None:
    if price.status == PriceStatus.FREE:
        return (0, 0, 0)
    if price.status == PriceStatus.VERIFIED and price.amount_yuan is not None:
        return (price.amount_yuan, price.amount_yuan, price.amount_yuan)
    if price.status == PriceStatus.ESTIMATED_RANGE:
        fallback = price.amount_yuan
        minimum = price.minimum_yuan if price.minimum_yuan is not None else fallback
        maximum = price.maximum_yuan if price.maximum_yuan is not None else fallback
        if minimum is not None and maximum is not None:
            return (None, min(minimum, maximum), max(minimum, maximum))
    return None


def _combine_prices(prices: list[PriceEvidence]) -> PriceEvidence:
    bounds = [_bounds(price) for price in prices]
    valid = [item for item in bounds if item is not None]
    all_confirmed = all(item[0] is not None for item in valid)
    source_urls = [price.source_url for price in prices if price.source_url]
    return PriceEvidence(
        status=PriceStatus.VERIFIED if all_confirmed else PriceStatus.ESTIMATED_RANGE,
        amount_yuan=sum(item[0] or 0 for item in valid) if all_confirmed else None,
        minimum_yuan=None if all_confirmed else sum(item[1] for item in valid),
        maximum_yuan=None if all_confirmed else sum(item[2] for item in valid),
        source_url=source_urls[0] if len(source_urls) == 1 else None,
        notes=["交通费用由已选择的行程段汇总"],
    )
