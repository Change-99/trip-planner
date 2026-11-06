"""约束规划使用的结构化领域模型。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.models.schemas import Location, WeatherInfo


def utc_now() -> datetime:
    """返回带时区的 UTC 时间，便于记录证据获取时间。"""

    return datetime.now(timezone.utc)


class EvidenceStatus(str, Enum):
    """外部证据的获取状态。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class VerificationStatus(str, Enum):
    """字段的可信状态。"""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PriceStatus(str, Enum):
    """价格的可信状态。"""

    VERIFIED = "verified"
    ESTIMATED_RANGE = "estimated_range"
    FREE = "free"
    UNKNOWN = "unknown"


class PriceCategory(str, Enum):
    """价格工具支持的消费类别。"""

    TICKET = "ticket"
    HOTEL_NIGHT = "hotel_night"
    MEAL_PERSON = "meal_person"
    TRANSPORT = "transport"


class TravelMode(str, Enum):
    """支持的出行方式。"""

    WALKING = "walking"
    DRIVING = "driving"
    TRANSIT = "transit"


class TaskType(str, Enum):
    """Task Planner 可以交给 Executor 的工具任务白名单。"""

    SEARCH_POI = "search_poi"
    SEARCH_HOTEL = "search_hotel"
    QUERY_WEATHER = "query_weather"
    QUERY_ROUTE = "query_route"
    QUERY_TICKET_PRICE = "query_ticket_price"
    QUERY_HOTEL_PRICE = "query_hotel_price"
    QUERY_MEAL_PRICE = "query_meal_price"


class PlanningStatus(str, Enum):
    """整个规划工作流的状态。"""

    COLLECTING_EVIDENCE = "collecting_evidence"
    READY_TO_PLAN = "ready_to_plan"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    LIMITED = "limited"


class EvidenceSource(BaseModel):
    """描述一条数据来自哪里以及何时获取。"""

    provider: str
    method: str
    fetched_at: datetime = Field(default_factory=utc_now)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TimeWindow(BaseModel):
    """一天内的时间窗口，采用 HH:MM 格式。"""

    start: str
    end: str


class OpeningHoursEvidence(BaseModel):
    """按星期保存的开放时间证据，0 表示星期一。"""

    status: VerificationStatus = VerificationStatus.UNKNOWN
    weekly_windows: dict[int, list[TimeWindow]] = Field(default_factory=dict)
    raw_text: str | None = None
    holiday_exception: bool = False
    notes: list[str] = Field(default_factory=list)


class PriceEvidence(BaseModel):
    """价格证据；未知价格绝不能用 0 代替。"""

    status: PriceStatus = PriceStatus.UNKNOWN
    amount_yuan: float | None = Field(default=None, ge=0)
    minimum_yuan: float | None = Field(default=None, ge=0)
    maximum_yuan: float | None = Field(default=None, ge=0)
    raw_text: str | None = None
    notes: list[str] = Field(default_factory=list)
    currency: str = "CNY"
    unit: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source: EvidenceSource | None = None
    applicable_date: date | None = None


class PriceQuoteEvidence(BaseModel):
    """价格 MCP 返回的领域化报价，不把搜索片段直接交给 LLM。"""

    quote_id: str
    category: PriceCategory
    entity_id: str | None = None
    entity_name: str
    price: PriceEvidence = Field(default_factory=PriceEvidence)
    source: EvidenceSource
    source_url: str | None = None
    raw_excerpt: str | None = None


class POIEvidence(BaseModel):
    """经过标准化的 POI 证据。"""

    poi_id: str
    name: str
    address: str = ""
    location: Location
    categories: list[str] = Field(default_factory=list)
    telephone: str | None = None
    rating: float | None = None
    opening_hours: OpeningHoursEvidence = Field(default_factory=OpeningHoursEvidence)
    ticket_price: PriceEvidence = Field(default_factory=PriceEvidence)
    reference_price: PriceEvidence = Field(default_factory=PriceEvidence)
    source: EvidenceSource
    raw_data: dict[str, Any] = Field(default_factory=dict, exclude=True)


class RouteEvidence(BaseModel):
    """两点之间的真实路线耗时和距离证据。"""

    origin_id: str
    destination_id: str
    mode: TravelMode
    distance_meters: int = Field(ge=0)
    duration_seconds: int = Field(ge=0)
    estimated_cost: PriceEvidence = Field(default_factory=PriceEvidence)
    description: str = ""
    source: EvidenceSource

    @property
    def duration_minutes(self) -> int:
        """向上取整后的交通分钟数。"""

        return (self.duration_seconds + 59) // 60


class WeatherEvidence(BaseModel):
    """天气查询证据。"""

    city: str
    forecasts: list[WeatherInfo] = Field(default_factory=list)
    status: EvidenceStatus
    source: EvidenceSource
    notes: list[str] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    """Executor 执行一个任务后返回的通用证据记录。"""

    task_id: str
    evidence_type: str
    status: EvidenceStatus
    data: dict[str, Any] = Field(default_factory=dict)
    source: EvidenceSource
    verified_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    error: str | None = None


class EvidenceBundle(BaseModel):
    """一次旅行规划已经收集到的全部结构化证据。"""

    pois: list[POIEvidence] = Field(default_factory=list)
    hotels: list[POIEvidence] = Field(default_factory=list)
    routes: list[RouteEvidence] = Field(default_factory=list)
    weather: WeatherEvidence | None = None
    price_quotes: list[PriceQuoteEvidence] = Field(default_factory=list)
    records: list[EvidenceRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PlanningConstraints(BaseModel):
    """从用户请求中提取出的显式规划约束。"""

    city: str
    start_date: date
    end_date: date
    travel_days: int = Field(ge=1, le=30)
    transportation: TravelMode = TravelMode.TRANSIT
    accommodation: str
    preferences: list[str] = Field(default_factory=list)
    additional_requirements: str = ""
    budget_limit_yuan: float | None = Field(default=None, ge=0)
    must_visit: list[str] = Field(default_factory=list)
    forbidden_places: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    daily_start_minute: int = Field(default=9 * 60, ge=0, le=24 * 60 - 1)
    daily_end_minute: int = Field(default=20 * 60, ge=1, le=24 * 60)
    pace: str = "normal"
    traveler_count: int = Field(default=1, ge=1, le=30)
    room_count: int = Field(default=1, ge=1, le=10)


class TaskSpec(BaseModel):
    """Task Planner 生成的一项可执行任务。"""

    task_id: str
    task_type: TaskType
    arguments: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    critical: bool = True
    reason: str = ""


class TaskPlan(BaseModel):
    """一轮工具任务计划。"""

    round_index: int = 0
    tasks: list[TaskSpec] = Field(default_factory=list)
    rationale: str = ""


class TaskResult(BaseModel):
    """Executor 执行一项任务后的结构化结果。"""

    task_id: str
    task_type: TaskType
    status: EvidenceStatus
    evidence_type: str
    data: Any = None
    source: EvidenceSource
    error: str | None = None


class EvidenceReport(BaseModel):
    """Evidence Checker 对现有证据是否足够的判断。"""

    sufficient: bool = False
    usable_poi_ids: list[str] = Field(default_factory=list)
    unusable_poi_ids: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    can_retry: bool = True
    next_action: str = "supplement"


class CandidateScore(BaseModel):
    """LLM 或确定性规则给候选景点计算的兴趣分。"""

    poi_id: str
    interest_score: int = Field(ge=0, le=100)
    reason: str = ""


class ScheduledActivity(BaseModel):
    """已经放入时间轴的一项活动。"""

    activity_type: str
    poi_id: str | None = None
    name: str
    start_minute: int = Field(ge=0, le=24 * 60)
    end_minute: int = Field(ge=0, le=24 * 60)
    location: Location | None = None
    price: PriceEvidence = Field(default_factory=PriceEvidence)
    evidence_source: EvidenceSource | None = None


class ScheduledDay(BaseModel):
    """一天的可执行时间轴。"""

    date: date
    activities: list[ScheduledActivity] = Field(default_factory=list)
    travel_minutes: int = 0
    active_minutes: int = 0


class ScoreBreakdown(BaseModel):
    """优化目标各组成部分。"""

    interest: float = 0
    transportation: float = 0
    cost_penalty: float = 0
    fatigue: float = 0
    total: float = 0


class OptimizedItinerary(BaseModel):
    """Python 约束求解器产生的行程草稿。"""

    city: str
    days: list[ScheduledDay] = Field(default_factory=list)
    selected_poi_ids: list[str] = Field(default_factory=list)
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    solver_status: str
    limitations: list[str] = Field(default_factory=list)


class BudgetComponent(BaseModel):
    """预算中的一个可追溯组成部分。"""

    category: PriceCategory
    label: str
    status: PriceStatus
    confirmed_yuan: float | None = Field(default=None, ge=0)
    minimum_yuan: float | None = Field(default=None, ge=0)
    maximum_yuan: float | None = Field(default=None, ge=0)
    source_urls: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BudgetAssessment(BaseModel):
    """只汇总有来源的金额；未知项目不会被当成 0 元。"""

    status: str = "unavailable"
    confirmed_total_yuan: float | None = Field(default=None, ge=0)
    estimated_minimum_yuan: float | None = Field(default=None, ge=0)
    estimated_maximum_yuan: float | None = Field(default=None, ge=0)
    components: list[BudgetComponent] = Field(default_factory=list)
    unknown_items: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdviceFact(BaseModel):
    """建议生成器可以使用的白名单事实。"""

    fact_id: str
    text: str
    source_type: str
    source_url: str | None = None


class AdviceItem(BaseModel):
    """带事实引用的单条旅行建议。"""

    category: str
    content: str
    evidence_refs: list[str] = Field(default_factory=list)


class AdviceDraft(BaseModel):
    """Advice Generator 的候选输出，必须再经过 Grounder。"""

    planning_summary: str
    facts: list[AdviceFact] = Field(default_factory=list)
    items: list[AdviceItem] = Field(default_factory=list)


class GroundedAdvice(BaseModel):
    """事实引用检查通过后的建议。"""

    planning_summary: str
    items: list[AdviceItem] = Field(default_factory=list)
    rejected_count: int = 0


class ConstraintViolation(BaseModel):
    """Validator 发现的一项约束违规。"""

    code: str
    message: str
    hard: bool = True
    related_ids: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """对行程草稿进行独立校验后的报告。"""

    passed: bool = False
    hard_violations: list[ConstraintViolation] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    repair_target: str = "stop"


class RepairDirective(BaseModel):
    """Replanner 输出的下一步动作，不直接伪造新行程。"""

    action: str
    reasons: list[str] = Field(default_factory=list)
    excluded_poi_ids: list[str] = Field(default_factory=list)
    task_hints: list[TaskSpec] = Field(default_factory=list)
