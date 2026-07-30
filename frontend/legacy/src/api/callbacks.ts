import { apiRequest } from "./webMessages"

export type CallbackStatus = "pending" | "retrying" | "done" | "dead"
export type CallbackEvent = "batch.finished" | "message.report"

export interface CallbackTask {
  id: number
  event_id: string
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
  items: CallbackTask[]
}

export function listCallbacks(status: CallbackStatus | "", page: number): Promise<CallbackPage> {
  const query = new URLSearchParams({ page: String(page) })
  if (status) query.set("status", status)
  return apiRequest<CallbackPage>(`/admin/callbacks?${query}`, { method: "GET" })
}

export function retryCallback(id: number): Promise<void> {
  return apiRequest<void>(`/admin/callbacks/${id}/retry`, { method: "POST" })
}
