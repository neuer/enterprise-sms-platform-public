import { apiRequest, apiRequestAbs } from "./client"
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import type { Page } from "./pagination"
import type { components, paths } from "./types.gen"

/** 批次契约：openapi.yaml components.schemas.Batch（status 为契约批次状态枚举）。 */
export type BatchItem = components["schemas"]["Batch"]

export interface BatchPage extends Page<BatchItem> {
  status_counts: Record<string, number>
}

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

/** 批次明细契约：openapi.yaml components.schemas.MessageDetail。 */
export type BatchMessage = components["schemas"]["MessageDetail"]

export type BatchMessagePage = Page<BatchMessage>

/** `/api/v1/web/messages` 200 响应（号码搜索结果），MessageItem/PhoneBadge 的契约来源。 */
type MessageSearchResult = paths["/api/v1/web/messages"]["post"]["responses"]["200"]["content"]["application/json"]

export type MessageItem = MessageSearchResult["items"][number]

export type PhoneBadge = MessageSearchResult["badge"]

export interface MessagePage extends Page<MessageItem> {
  badge: PhoneBadge
}

/** 号码时间线契约：`/api/v1/web/messages/timeline` 200 响应。 */
export type TimelineResult =
  paths["/api/v1/web/messages/timeline"]["post"]["responses"]["200"]["content"]["application/json"]

export type TimelineEvent = TimelineResult["events"][number]

export function listBatches(filters: BatchFilters, signal?: AbortSignal): Promise<BatchPage> {
  const query = new URLSearchParams({ page: String(filters.page), size: String(DEFAULT_PAGE_SIZE) })
  if (filters.category) query.set("category", filters.category)
  if (filters.status) query.set("status", filters.status)
  if (filters.is_test !== undefined) query.set("is_test", String(filters.is_test))
  if (filters.channel) query.set("channel", filters.channel)
  if (filters.app_id) query.set("app_id", String(filters.app_id))
  if (filters.dept) query.set("dept", filters.dept)
  if (filters.batch_no) query.set("batch_no", filters.batch_no)
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<BatchPage>(`/batches?${query}`, { method: "GET", signal })
}

export function getBatch(batchNo: string, signal?: AbortSignal): Promise<BatchItem> {
  return apiRequestAbs<BatchItem>(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}`, { method: "GET", signal })
}

export interface BatchMessageFilters {
  status?: string
  page?: number
}

export function getBatchMessages(
  batchNo: string,
  filters: BatchMessageFilters = {},
  signal?: AbortSignal,
): Promise<BatchMessagePage> {
  const query = new URLSearchParams({ page: String(filters.page ?? 1), size: String(DEFAULT_PAGE_SIZE) })
  if (filters.status) query.set("status", filters.status)
  return apiRequestAbs<BatchMessagePage>(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/details?${query}`, {
    method: "GET",
    signal,
  })
}

export interface MessageSearchFilters {
  start?: string
  end?: string
  category?: string
  status?: string
  page?: number
}

function jsonPost<T>(
  path: string,
  body: Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<T> {
  const payload: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined) payload[key] = value
  }
  return apiRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  })
}

export function searchMessages(
  phone: string,
  filters: MessageSearchFilters = {},
  signal?: AbortSignal,
): Promise<MessagePage> {
  return jsonPost<MessagePage>(
    "/messages",
    {
      phone,
      start: filters.start,
      end: filters.end,
      category: filters.category,
      status: filters.status,
      page: filters.page,
    },
    signal,
  )
}

export function getTimeline(
  phone: string,
  start?: string,
  end?: string,
  signal?: AbortSignal,
): Promise<TimelineResult> {
  return jsonPost<TimelineResult>("/messages/timeline", { phone, start, end }, signal)
}

export function decryptMessagePhone(id: number): Promise<{ phone: string }> {
  return apiRequest<{ phone: string }>(`/messages/${id}/phone/decrypt`, { method: "POST" })
}

export function cancelBatch(batchNo: string): Promise<void> {
  return apiRequestAbs(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/cancel`, { method: "POST" })
}

export function rescheduleBatch(batchNo: string, scheduledAt: string): Promise<void> {
  return apiRequestAbs(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/reschedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scheduled_at: scheduledAt }),
  })
}

export function resendFailedBatch(
  batchNo: string,
): Promise<{ batch_no: string; resend_of: string; accepted: number; status: string }> {
  return apiRequestAbs(`/api/v1/messages/batches/${encodeURIComponent(batchNo)}/resend-failed`, { method: "POST" })
}
