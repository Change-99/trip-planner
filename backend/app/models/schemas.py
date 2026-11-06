"""数据模型定义"""

from typing import List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import date


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    city: str = Field(..., description="目的地城市", json_schema_extra={"example": "北京"})
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD", json_schema_extra={"example": "2025-06-01"})
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD", json_schema_extra={"example": "2025-06-03"})
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30, json_schema_extra={"example": 3})
    transportation: str = Field(..., description="交通方式", json_schema_extra={"example": "公共交通"})
    accommodation: str = Field(..., description="住宿偏好", json_schema_extra={"example": "经济型酒店"})
    preferences: List[str] = Field(
        default_factory=list,
        description="旅行偏好标签",
        json_schema_extra={"example": ["历史文化", "美食"]},
    )
    free_text_input: Optional[str] = Field(
        default="",
        description="额外要求",
        json_schema_extra={"example": "希望多安排一些博物馆"},
    )
    budget_limit: Optional[float] = Field(default=None, ge=0, description="总预算上限（元）")
    traveler_count: int = Field(default=1, ge=1, le=30, description="出行人数")
    room_count: int = Field(default=1, ge=1, le=10, description="住宿房间数")
    must_visit: List[str] = Field(default_factory=list, description="必须安排的景点")
    forbidden_places: List[str] = Field(default_factory=list, description="明确禁止安排的地点")
    forbidden_categories: List[str] = Field(default_factory=list, description="明确禁止安排的类别")
    daily_start_time: str = Field(default="09:00", description="每天最早开始时间 HH:MM")
    daily_end_time: str = Field(default="20:00", description="每天最晚结束时间 HH:MM")
    pace: Literal["relaxed", "normal", "intensive"] = Field(
        default="normal",
        description="行程节奏：轻松、普通或紧凑",
    )
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆",
            }
        }
    )

class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", json_schema_extra={"example": "故宫"})
    city: str = Field(..., description="城市", json_schema_extra={"example": "北京"})
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", json_schema_extra={"example": "北京市朝阳区阜通东大街6号"})
    destination_address: str = Field(..., description="终点地址", json_schema_extra={"example": "北京市海淀区上地十街10号"})
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: Optional[int] = Field(
        default=None,
        description="已核实的门票价格(元)；未知时为 null",
    )


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: Optional[int] = Field(default=None, description="已核实或明确标注的费用(元)")


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: Optional[int] = Field(default=None, description="已核实或明确标注的费用(元/晚)")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default_factory=list, description="景点列表")
    meals: List[Meal] = Field(default_factory=list, description="餐饮列表")
    timeline: List[dict] = Field(default_factory=list, description="包含交通和用餐的时间轴")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class BudgetComponentView(BaseModel):
    """带来源状态的预算组成部分。"""

    category: str
    label: str
    status: str
    confirmed_yuan: Optional[float] = None
    minimum_yuan: Optional[float] = None
    maximum_yuan: Optional[float] = None
    source_urls: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class Budget(BaseModel):
    """部分预算；未知金额不使用 0 填充。"""

    status: str = Field(default="unavailable", description="verified/partial/unavailable")
    confirmed_total_yuan: Optional[float] = None
    estimated_minimum_yuan: Optional[float] = None
    estimated_maximum_yuan: Optional[float] = None
    components: List[BudgetComponentView] = Field(default_factory=list)
    unknown_items: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class TravelAdvice(BaseModel):
    """经过事实引用校验的旅行建议。"""

    category: str
    content: str
    evidence_refs: List[str] = Field(default_factory=list)


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default_factory=list, description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    planning_summary: str = Field(default="", description="确定性的规划说明")
    travel_advice: List[TravelAdvice] = Field(default_factory=list, description="有事实依据的旅行建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")
    planning_status: str = Field(default="completed", description="约束规划状态")
    limitations: List[str] = Field(default_factory=list, description="数据与规划限制")
    score_breakdown: dict = Field(default_factory=dict, description="优化目标分解")


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    plan_id: Optional[str] = Field(default=None, description="后端持久化计划 ID")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")
    planning_status: str = Field(default="", description="约束规划状态")
    limitations: List[str] = Field(
        default_factory=list,
        description="无法规划或数据不完整时的明确原因",
    )


class TripJobStarted(BaseModel):
    """已提交的异步规划任务。"""

    job_id: str
    status: str = "queued"
    stage: str = "已提交规划任务"
    progress: int = 0


class TripJobStatus(BaseModel):
    """规划任务的真实执行状态。"""

    job_id: str
    status: str
    stage: str
    progress: int = Field(ge=0, le=100)
    plan_id: Optional[str] = None
    error: Optional[str] = None


class StoredTripPlanResponse(BaseModel):
    """从后端持久化存储中读取的计划修订。"""

    plan_id: str
    request: TripRequest
    data: TripPlan
    revision: int = 1
    parent_plan_id: Optional[str] = None
    created_at: str
    updated_at: str


class TripReplanRequest(BaseModel):
    """将用户编辑转换为新的规划约束，而不是直接改写结果 JSON。"""

    must_visit: List[str] = Field(default_factory=list)
    forbidden_places: List[str] = Field(default_factory=list)
    additional_requirements: str = ""


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")
