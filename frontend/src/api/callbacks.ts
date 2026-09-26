import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { apiRequest } from "./client"
import type { Page } from "./pagination"
import type { paths } from "./types.gen"

/**
 * 回调任务列表项：openapi 未提供命名 schema，逐字段对应
 * GET /api/v1/web/admin/callbacks 200 响应 items 的内联元素类型。
 */
export type CallbackTask =
  paths["/api/v1/web/admin/callbacks"]["get"]["responses"]["200"]["content"]["application/json"]["items"][number]
export type CallbackStatus = CallbackTask["status"]
export type CallbackEvent = CallbackTask["event"]

export interface CallbackPage extends Page<CallbackTask> {
  dead_total: number
}

export interface CallbackFilters {
  status?: CallbackStatus | ""
  appId?: number | null
  event?: CallbackEvent | ""
  batchNo?: string
  page: number
}

export function listCallbacks(filters: CallbackFilters, signal?: AbortSignal): Promise<CallbackPage> {
  const query = new URLSearchParams({ page: String(filters.page), size: String(DEFAULT_PAGE_SIZE) })
  if (filters.status) query.set("status", filters.status)
  if (filters.appId) query.set("app_id", String(filters.appId))
  if (filters.event) query.set("event", filters.event)
  if (filters.batchNo?.trim()) query.set("batch_no", filters.batchNo.trim())
  return apiRequest<CallbackPage>(`/admin/callbacks?${query}`, { method: "GET", signal })
}

export function retryCallback(id: number): Promise<void> {
  return apiRequest<void>(`/admin/callbacks/${id}/retry`, { method: "POST" })
}
