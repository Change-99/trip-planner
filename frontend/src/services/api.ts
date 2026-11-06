import axios from 'axios'
import type {
  StoredTripPlan,
  TripFormData,
  TripJobStarted,
  TripJobStatus,
  TripPlanResponse,
  TripReplanRequest
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000, // 2分钟超时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

/** 提交规划任务，立即返回 job_id。 */
export async function startTripPlanJob(formData: TripFormData): Promise<TripJobStarted> {
  try {
    const response = await apiClient.post<TripJobStarted>('/api/trip/jobs', formData)
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '提交规划任务失败')
  }
}

/** 读取后端记录的真实 LangGraph 节点进度。 */
export async function getTripJob(jobId: string): Promise<TripJobStatus> {
  const response = await apiClient.get<TripJobStatus>(`/api/trip/jobs/${jobId}`)
  return response.data
}

export async function waitForTripJob(
  jobId: string,
  onProgress: (status: TripJobStatus) => void,
  timeoutMs = 120000
): Promise<TripJobStatus> {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    const status = await getTripJob(jobId)
    onProgress(status)
    if (status.status === 'completed') return status
    if (status.status === 'failed') {
      throw new Error(status.error || '旅行规划任务失败')
    }
    await new Promise(resolve => window.setTimeout(resolve, 800))
  }
  throw new Error('查询规划结果超时；后端任务仍可能在继续执行')
}

/** 按 plan_id 从后端 SQLite 读取，不依赖浏览器缓存。 */
export async function getStoredTripPlan(planId: string): Promise<StoredTripPlan> {
  try {
    const response = await apiClient.get<StoredTripPlan>(`/api/trip/plans/${planId}`)
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '读取旅行计划失败')
  }
}

/** 将编辑内容转成约束并创建新修订，不直接篡改结果。 */
export async function startReplanJob(
  planId: string,
  changes: TripReplanRequest
): Promise<TripJobStarted> {
  try {
    const response = await apiClient.post<TripJobStarted>(
      `/api/trip/plans/${planId}/replan`,
      changes
    )
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '提交重新规划失败')
  }
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient
