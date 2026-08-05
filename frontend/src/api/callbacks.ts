import { apiRequest } from "./webMessages"

export type CallbackStatus = "pending" | "retrying" | "done" | "dead"
export type CallbackEvent = "batch.finished" | "message.report"

export interface CallbackTask {
  id: number
  event_id: string
  correlation_id: string
  app_id: number
  app_name: string
  event: CallbackEvent
  batch_no: string | null
  reference_count: number
  status: CallbackStatus
  retry_count: number
  next_retry_at: string | null
  lease_id: string | null
  lease_expires_at: string | null
  takeover_count: number
  stalled: boolean
  last_http_code: number | null
  last_error: string | null
  created_at: string
  finished_at: string | null
}

export interface CallbackPage {
  total: number
  dead_total: number
  items: CallbackTask[]
}

export interface CallbackFilters {
  status?: CallbackStatus | ""
  appId?: number | null
  event?: CallbackEvent | ""
  batchNo?: string
  page: number
}

export function listCallbacks(filters: CallbackFilters): Promise<CallbackPage> {
  const query = new URLSearchParams({ page: String(filters.page) })
  if (filters.status) query.set("status", filters.status)
  if (filters.appId) query.set("app_id", String(filters.appId))
  if (filters.event) query.set("event", filters.event)
  if (filters.batchNo?.trim()) query.set("batch_no", filters.batchNo.trim())
  return apiRequest<CallbackPage>(`/admin/callbacks?${query}`, { method: "GET" })
}

export function retryCallback(id: number): Promise<void> {
  return apiRequest<void>(`/admin/callbacks/${id}/retry`, { method: "POST" })
}
