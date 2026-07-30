import { apiRequest, authorizedFetch } from "./webMessages"

export interface BatchItem {
  batch_no: string
  category: "verify" | "notice" | "market"
  channel: "api" | "web"
  app_name: string | null
  creator: string | null
  dept: string
  content: string
  status: string
  deferred_reason: string | null
  resend_of: string | null
  is_test: boolean
  segments: number
  quota_cost: number
  total: number
  removed_freq_limit: number
  delivered: number
  failed: number
  unknown: number
  scheduled_at: string | null
  created_at: string
}

export interface BatchPage { total: number; items: BatchItem[] }

export interface BatchFilters {
  category?: string
  status?: string
  is_test?: boolean
  channel?: string
  app_id?: number
  dept?: string
  start?: string
  end?: string
  page: number
}

export interface BatchMessage {
  id: number
  phone: string
  status: string
  vendor_task_id: string | null
  report_desc: string | null
  report_time: string | null
}

export interface BatchMessagePage { total: number; items: BatchMessage[] }

export interface MessageItem {
  id: number
  phone: string
  status: string
  report_desc: string | null
  report_time: string | null
  created_at: string
  batch_no: string
  category: string
  content: string
  sender: string | null
}

export interface MessagePage { total: number; items: MessageItem[] }

export interface TimelineEvent {
  ts: string
  direction: "out" | "in"
  category: string | null
  batch_no: string | null
  content: string
  status: string | null
  sender: string | null
}

export interface TimelineResult {
  badge: { blacklisted: boolean; blacklist_source: string | null; recv_30d: number }
  events: TimelineEvent[]
}

async function directRequest<T>(url: string, init: RequestInit = { method: "GET" }): Promise<T> {
  const response = await authorizedFetch(url, init)
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
  return (await response.json()) as T
}

async function directVoid(url: string, init: RequestInit): Promise<void> {
  const response = await authorizedFetch(url, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { message?: string; code?: string }
    throw new Error(body.message || body.code || `请求失败（${response.status}）`)
  }
}

export function listBatches(filters: BatchFilters): Promise<BatchPage> {
  const query = new URLSearchParams({ page: String(filters.page), size: "20" })
  if (filters.category) query.set("category", filters.category)
  if (filters.status) query.set("status", filters.status)
  if (filters.is_test !== undefined) query.set("is_test", String(filters.is_test))
  if (filters.channel) query.set("channel", filters.channel)
  if (filters.app_id) query.set("app_id", String(filters.app_id))
  if (filters.dept) query.set("dept", filters.dept)
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<BatchPage>(`/batches?${query}`, { method: "GET" })
}

export function getBatch(batchNo: string): Promise<BatchItem> {
  return directRequest<BatchItem>(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}`)
}

export function getBatchMessages(batchNo: string): Promise<BatchMessagePage> {
  return directRequest<BatchMessagePage>(
    `/api/v1/messages/batches/${encodeURIComponent(batchNo)}/details?page=1&size=100`,
  )
}

function phoneQuery(phone: string, start?: string, end?: string, page?: number): string {
  const query = new URLSearchParams({ phone })
  if (start) query.set("start", start)
  if (end) query.set("end", end)
  if (page) query.set("page", String(page))
  return query.toString()
}

export function searchMessages(phone: string, start?: string, end?: string, page = 1): Promise<MessagePage> {
  return apiRequest<MessagePage>(`/messages?${phoneQuery(phone, start, end, page)}`, { method: "GET" })
}

export function getTimeline(phone: string, start?: string, end?: string): Promise<TimelineResult> {
  return apiRequest<TimelineResult>(`/messages/timeline?${phoneQuery(phone, start, end)}`, { method: "GET" })
}

export function decryptMessagePhone(id: number): Promise<{ phone: string }> {
  return apiRequest<{ phone: string }>(`/messages/${id}/phone/decrypt`, { method: "POST" })
}

export function cancelBatch(batchNo: string): Promise<void> {
  return directVoid(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/cancel`, { method: "POST" })
}

export function rescheduleBatch(batchNo: string, scheduledAt: string): Promise<void> {
  return directVoid(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/reschedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scheduled_at: scheduledAt }),
  })
}

export function resendFailedBatch(batchNo: string): Promise<{ batch_no: string; resend_of: string; accepted: number; status: string }> {
  return directRequest(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/resend-failed`, { method: "POST" })
}
