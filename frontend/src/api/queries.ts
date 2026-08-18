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
  batch_no?: string
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
  truncated: boolean
}

async function throwResponseError(response: Response): Promise<never> {
  const body = await response.json().catch(() => ({})) as { message?: string; code?: string }
  throw new Error(body.message || body.code || `请求失败（${response.status}）`)
}

async function directRequest<T>(url: string, init: RequestInit = { method: "GET" }): Promise<T> {
  const response = await authorizedFetch(url, init)
  if (!response.ok) await throwResponseError(response)
  return (await response.json()) as T
}

async function directVoid(url: string, init: RequestInit): Promise<void> {
  const response = await authorizedFetch(url, init)
  if (!response.ok) await throwResponseError(response)
}

export function listBatches(filters: BatchFilters): Promise<BatchPage> {
  const query = new URLSearchParams({ page: String(filters.page), size: "20" })
  if (filters.category) query.set("category", filters.category)
  if (filters.status) query.set("status", filters.status)
  if (filters.is_test !== undefined) query.set("is_test", String(filters.is_test))
  if (filters.channel) query.set("channel", filters.channel)
  if (filters.app_id) query.set("app_id", String(filters.app_id))
  if (filters.dept) query.set("dept", filters.dept)
  if (filters.batch_no) query.set("batch_no", filters.batch_no)
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<BatchPage>(`/batches?${query}`, { method: "GET" })
}

export function getBatch(batchNo: string): Promise<BatchItem> {
  return directRequest<BatchItem>(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}`)
}

export interface BatchMessageFilters {
  status?: string
  page?: number
}

export function getBatchMessages(
  batchNo: string,
  filters: BatchMessageFilters = {},
): Promise<BatchMessagePage> {
  const query = new URLSearchParams({ page: String(filters.page ?? 1), size: "20" })
  if (filters.status) query.set("status", filters.status)
  return directRequest<BatchMessagePage>(
    `/api/v1/messages/batches/${encodeURIComponent(batchNo)}/details?${query}`,
  )
}

export interface MessageSearchFilters {
  start?: string
  end?: string
  category?: string
  status?: string
  page?: number
}

function jsonPost<T>(path: string, body: Record<string, string | number | undefined>): Promise<T> {
  const payload: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined) payload[key] = value
  }
  return apiRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}

export function searchMessages(
  phone: string,
  filters: MessageSearchFilters = {},
): Promise<MessagePage> {
  return jsonPost<MessagePage>("/messages", {
    phone,
    start: filters.start,
    end: filters.end,
    category: filters.category,
    status: filters.status,
    page: filters.page,
  })
}

export function getTimeline(phone: string, start?: string, end?: string): Promise<TimelineResult> {
  return jsonPost<TimelineResult>("/messages/timeline", { phone, start, end })
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
