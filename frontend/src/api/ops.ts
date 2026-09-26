import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import type { ExportTask } from "./reports"
import type { NumberedPage } from "./pagination"
import type { components } from "./types.gen"
import { apiRequest } from "./client"

// 与 api/pagination.ts 的 NumberedPage<T> 同形（items/total/page/page_size），别名引用单点。
export type OpsPage<T> = NumberedPage<T>
// 以下运维列表/详情类型与 openapi.yaml components.schemas 逐字段一致，直接别名引用生成契约。
export type AlertItem = components["schemas"]["AlertModel"]
export type CurrentAlertItem = components["schemas"]["CurrentAlertModel"]
export type CurrentAlertSnapshot = components["schemas"]["CurrentAlertSnapshotModel"]
export type RawLogItem = components["schemas"]["RawLogModel"]
export type RawCaptureState = RawLogItem["capture_state"]
export type RawParseState = RawLogItem["parse_state"]
export type RawReplayEligibility = RawLogItem["replay_eligibility"]
export type RawReevaluateResult = components["schemas"]["ReevaluateResultModel"]
export type UncertainResolutionAction = components["schemas"]["UncertainResolutionRequestModel"]["action"]
export type UncertainResolutionState = components["schemas"]["UncertainResolutionModel"]["state"]
export type UncertainItem = components["schemas"]["UncertainModel"]
export type UnmatchedItem = components["schemas"]["UnmatchedModel"]
export type JobItem = components["schemas"]["JobModel"]
export type QueueStatus = components["schemas"]["QueueStatusModel"]
export type QueueResumeResult = components["schemas"]["QueueResumeModel"]
export type OutboxStats = components["schemas"]["OutboxStatsModel"]
export type OutboxState = components["schemas"]["OutboxEventModel"]["state"]
export type OutboxEventItem = components["schemas"]["OutboxEventModel"]

export interface PageQuery {
  page?: number
  pageSize?: number
}
export interface AlertQuery extends PageQuery {
  alertType?: string
  level?: AlertItem["level"]
  start?: string
  end?: string
}
export interface RawLogQuery extends PageQuery {
  source?: RawLogItem["source"]
  processed?: boolean
}
export interface UnmatchedQuery extends PageQuery {
  phone?: string
  start?: string
  end?: string
}
export type UnmatchedExportFilters = Omit<UnmatchedQuery, "page" | "pageSize">

function pageParams(query: PageQuery): URLSearchParams {
  return new URLSearchParams({
    page: String(query.page ?? 1),
    page_size: String(query.pageSize ?? DEFAULT_PAGE_SIZE),
  })
}

export function listAlerts(query: AlertQuery = {}, signal?: AbortSignal): Promise<OpsPage<AlertItem>> {
  const params = pageParams(query)
  if (query.alertType?.trim()) params.set("alert_type", query.alertType.trim())
  if (query.level) params.set("level", query.level)
  if (query.start) params.set("start", query.start)
  if (query.end) params.set("end", query.end)
  return apiRequest<OpsPage<AlertItem>>(`/admin/alerts?${params}`, { method: "GET", signal })
}

export const getCurrentAlerts = (signal?: AbortSignal) =>
  apiRequest<CurrentAlertSnapshot>("/admin/alerts/current", { method: "GET", signal })

export function listRawLogs(query: RawLogQuery = {}, signal?: AbortSignal): Promise<OpsPage<RawLogItem>> {
  const params = pageParams(query)
  if (query.source) params.set("source", query.source)
  if (query.processed !== undefined) params.set("processed", String(query.processed))
  return apiRequest<OpsPage<RawLogItem>>(`/admin/raw-logs?${params}`, { method: "GET", signal })
}

export const replayRaw = (id: number) =>
  apiRequest<{ processed_items: number }>(`/admin/raw-logs/${id}/replay`, { method: "POST" })

export const reevaluateRaw = (id: number) =>
  apiRequest<RawReevaluateResult>(`/admin/raw-logs/${id}/reevaluate`, { method: "POST" })

export function listUncertain(query: PageQuery = {}, signal?: AbortSignal): Promise<OpsPage<UncertainItem>> {
  return apiRequest<OpsPage<UncertainItem>>(`/admin/chunks/uncertain?${pageParams(query)}`, { method: "GET", signal })
}

/**
 * 与生成契约 UncertainResolutionModel 的唯一差异：契约因服务端默认值 1 把
 * effect_generation 标为必选，本类型历史上标为可选；为不收紧既有消费方保留手写。
 */
export interface UncertainResolutionItem {
  id: number
  chunk_id: number
  batch_id: number
  action: string
  state: UncertainResolutionState
  proposer_account_id: number
  confirmer_account_id: number | null
  child_batch_id: number | null
  source_dept?: string | null
  source_channel?: string | null
  source_category?: string | null
  effect_generation?: number
  effect_error?: string | null
}

export const proposeUncertainResolution = (chunkId: number, action: UncertainResolutionAction) =>
  apiRequest<UncertainResolutionItem>(`/admin/chunks/${chunkId}/resolution`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  })

export const confirmUncertainResolution = (resolutionId: number) =>
  apiRequest<UncertainResolutionItem>(`/admin/resolutions/${resolutionId}/confirm`, {
    method: "POST",
  })

export function listUnmatched(query: UnmatchedQuery = {}, signal?: AbortSignal): Promise<OpsPage<UnmatchedItem>> {
  // 手机号精确查询条件只在请求体携带：GET query 会把明文写进访问日志（硬性规则 2）。
  return apiRequest<OpsPage<UnmatchedItem>>("/admin/unmatched-reports", {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      phone: query.phone?.trim() || null,
      start: query.start || null,
      end: query.end || null,
      page: query.page ?? 1,
      page_size: query.pageSize ?? DEFAULT_PAGE_SIZE,
    }),
  })
}
export const listJobs = (signal?: AbortSignal) => apiRequest<JobItem[]>("/admin/jobs", { method: "GET", signal })
export const triggerJob = (name: string) =>
  apiRequest<void>(`/admin/jobs/${encodeURIComponent(name)}/trigger`, { method: "POST" })
export const getQueueStatus = (signal?: AbortSignal) =>
  apiRequest<QueueStatus>("/admin/queue/status", { method: "GET", signal })
export const resumeQueue = (force: boolean) =>
  apiRequest<QueueResumeResult>(`/admin/queue/resume?force=${force}`, { method: "POST" })
export const getOutboxStatus = (signal?: AbortSignal) =>
  apiRequest<OutboxStats>("/admin/outbox", { method: "GET", signal })
export function listOutboxEvents(
  query: PageQuery & { state?: OutboxState },
  signal?: AbortSignal,
): Promise<OpsPage<OutboxEventItem>> {
  const params = pageParams(query)
  if (query.state) params.set("state", query.state)
  return apiRequest<OpsPage<OutboxEventItem>>(`/admin/outbox/events?${params}`, { method: "GET", signal })
}
export const retryOutboxEvent = (id: string) => apiRequest<void>(`/admin/outbox/${id}/retry`, { method: "POST" })
export function createUnmatchedExport(filters: UnmatchedExportFilters, decrypted: boolean): Promise<ExportTask> {
  return apiRequest<ExportTask>("/admin/unmatched-reports/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      phone: filters.phone?.trim() || null,
      start: filters.start || null,
      end: filters.end || null,
      decrypted,
    }),
  })
}
