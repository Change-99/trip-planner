// 类型定义

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number | null
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface Budget {
  status: 'verified' | 'partial' | 'unavailable'
  confirmed_total_yuan?: number | null
  estimated_minimum_yuan?: number | null
  estimated_maximum_yuan?: number | null
  components: BudgetComponent[]
  unknown_items: string[]
  notes: string[]
}

export interface BudgetComponent {
  category: string
  label: string
  status: string
  confirmed_yuan?: number | null
  minimum_yuan?: number | null
  maximum_yuan?: number | null
  source_urls: string[]
  notes: string[]
}

export interface TravelAdvice {
  category: string
  content: string
  evidence_refs: string[]
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
  timeline?: Array<{
    type: string
    name: string
    start_time: string
    end_time: string
    poi_id?: string
  }>
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  planning_summary: string
  travel_advice: TravelAdvice[]
  budget?: Budget
  planning_status?: string
  limitations?: string[]
  score_breakdown?: Record<string, number>
}

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
  budget_limit?: number
  traveler_count: number
  room_count: number
}

export interface TripPlanResponse {
  success: boolean
  message: string
  plan_id?: string
  data?: TripPlan
}

export interface TripJobStarted {
  job_id: string
  status: 'queued' | 'running'
  stage: string
  progress: number
}

export interface TripJobStatus {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  stage: string
  progress: number
  plan_id?: string
  error?: string
}

export interface StoredTripPlan {
  plan_id: string
  request: TripFormData & {
    must_visit?: string[]
    forbidden_places?: string[]
    forbidden_categories?: string[]
  }
  data: TripPlan
  revision: number
  parent_plan_id?: string
  created_at: string
  updated_at: string
}

export interface TripReplanRequest {
  must_visit?: string[]
  forbidden_places?: string[]
  additional_requirements?: string
}
