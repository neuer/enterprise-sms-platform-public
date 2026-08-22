import type { ExportTask } from "./reports"
import { apiRequest } from "./webMessages"

export interface OpsPage<T> { items: T[]; total: number; page: number; page_size: number }
export interface AlertItem { id: number; alert_type: string; level: "info" | "warn" | "crit"; title: string; detail: Record<string, unknown> | null; channels: string; created_at: string }
export type RawCaptureState = "complete" | "complete_too_large" | "truncated"
export interface RawLogItem {
  id: number
  source: "report" | "reply"
  item_count: number
  custom_id_count: number
  processed: boolean
  error: string | null
  fetched_at: string
  capture_state: RawCaptureState
}
export interface UncertainItem { chunk_id: number; batch_no: string; custom_id: string; phone_count: number; vendor_code: number | null; uncertain_since: string; age_seconds: number }
export interface UnmatchedItem { id: number; vendor_task_id: string | null; custom_id: string | null; phone_mask: string; report_status: number | null; report_desc: string | null; report_time: string | null; created_at: string }
export interface JobItem { job_name: string; last_run_at: string | null; last_status: "running" | "success" | "failed" | null; last_duration_ms: number | null; last_items: number; success_rate_24h: number; stalled: boolean }
export interface QueueStatus { realtime_code: string | null; bulk_code: string | null; balance: number | null; threshold: number }
export interface QueueResumeResult { resumed_batches: number; paused_codes: string[] }
export interface OutboxStats { pending: number; published: number; processing: number; dead: number; failed_attempts: number; oldest_age_seconds: number }
export type OutboxState = "pending" | "leased" | "published" | "processing" | "completed" | "dead"
export interface OutboxEventItem {
  id: string
  event_type: string
  aggregate_type: string
  aggregate_id: string
  task_name: string
  queue: string
  state: OutboxState
  attempts: number
  max_attempts: number
  failure_count: number
  last_error: string | null
  next_attempt_at: string
  created_at: string
  updated_at: string
}

export interface PageQuery { page?: number; pageSize?: number }
export interface AlertQuery extends PageQuery { alertType?: string; level?: AlertItem["level"]; start?: string; end?: string }
export interface RawLogQuery extends PageQuery { source?: RawLogItem["source"]; processed?: boolean }
export interface UnmatchedQuery extends PageQuery { phone?: string; start?: string; end?: string }
export type UnmatchedExportFilters = Omit<UnmatchedQuery, "page" | "pageSize">

function pageParams(query: PageQuery): URLSearchParams {
  return new URLSearchParams({
    page: String(query.page ?? 1),
    page_size: String(query.pageSize ?? 20),
  })
}

export function listAlerts(query: AlertQuery = {}): Promise<OpsPage<AlertItem>> {
  const params = pageParams(query)
  if (query.alertType?.trim()) params.set("alert_type", query.alertType.trim())
  if (query.level) params.set("level", query.level)
  if (query.start) params.set("start", query.start)
  if (query.end) params.set("end", query.end)
  return apiRequest<OpsPage<AlertItem>>(`/admin/alerts?${params}`, { method: "GET" })
}

export function listRawLogs(query: RawLogQuery = {}): Promise<OpsPage<RawLogItem>> {
  const params = pageParams(query)
  if (query.source) params.set("source", query.source)
  if (query.processed !== undefined) params.set("processed", String(query.processed))
  return apiRequest<OpsPage<RawLogItem>>(`/admin/raw-logs?${params}`, { method: "GET" })
}

export const replayRaw = (id: number) => apiRequest<{ processed_items: number }>(`/admin/raw-logs/${id}/replay`, { method: "POST" })

export function listUncertain(query: PageQuery = {}): Promise<OpsPage<UncertainItem>> {
  return apiRequest<OpsPage<UncertainItem>>(`/admin/chunks/uncertain?${pageParams(query)}`, { method: "GET" })
}

export function listUnmatched(query: UnmatchedQuery = {}): Promise<OpsPage<UnmatchedItem>> {
  const params = pageParams(query)
  if (query.phone?.trim()) params.set("phone", query.phone.trim())
  if (query.start) params.set("start", query.start)
  if (query.end) params.set("end", query.end)
  return apiRequest<OpsPage<UnmatchedItem>>(`/admin/unmatched-reports?${params}`, { method: "GET" })
}
export const listJobs = () => apiRequest<JobItem[]>("/admin/jobs", { method: "GET" })
export const triggerJob = (name: string) => apiRequest<void>(`/admin/jobs/${encodeURIComponent(name)}/trigger`, { method: "POST" })
export const getQueueStatus = () => apiRequest<QueueStatus>("/admin/queue/status", { method: "GET" })
export const resumeQueue = (force: boolean) => apiRequest<QueueResumeResult>(`/admin/queue/resume?force=${force}`, { method: "POST" })
export const getOutboxStatus = () => apiRequest<OutboxStats>("/admin/outbox", { method: "GET" })
export function listOutboxEvents(query: PageQuery & { state?: OutboxState }): Promise<OpsPage<OutboxEventItem>> {
  const params = pageParams(query)
  if (query.state) params.set("state", query.state)
  return apiRequest<OpsPage<OutboxEventItem>>(`/admin/outbox/events?${params}`, { method: "GET" })
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
