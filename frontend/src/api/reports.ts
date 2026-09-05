import { PASSWORD_AUTH_REQUEST_TIMEOUT_MS } from "./auth"
import { apiRequest, authorizedBlob, ApiRequestError, DOWNLOAD_TIMEOUT_MS } from "./client"

export type ReportGranularity = "day" | "week" | "month"
export type ReportGroupBy = "app" | "dept"
export type ReportCategory = "verify" | "notice" | "market" | "all"
/** 趋势堆叠可选指标：消息数或计费条。 */
export type ReportTrendMetric = "total" | "total_segments"

export interface ReportRow {
  period_start: string
  dim_value: string
  dim_label: string
  total: number
  total_segments: number
  delivered: number
  failed: number
  unknown: number
  success_rate: number
}

export interface ReportSummary {
  total: number
  total_segments: number
  delivered: number
  failed: number
  unknown: number
  success_rate: number
}

export interface ReportDimSummary {
  dim_value: string
  dim_label: string
  total: number
  total_segments: number
  delivered: number
  failed: number
  unknown: number
  success_rate: number
}

export interface ReportResult {
  granularity: ReportGranularity
  group_by: ReportGroupBy
  category: ReportCategory
  start: string
  end: string
  can_export_decrypted: boolean
  summary: ReportSummary
  dim_summary: ReportDimSummary[]
  items: ReportRow[]
}

export interface ExportTask {
  id: string
  status: "pending" | "running" | "done" | "failed"
  decrypted: boolean
  row_count: number | null
  download_url: string | null
  expires_at: string | null
  created_at: string
}

export interface ReportFilters {
  granularity: ReportGranularity
  groupBy: ReportGroupBy
  category: ReportCategory
  start: string
  end: string
}

export function getReport(filters: ReportFilters): Promise<ReportResult> {
  const query = new URLSearchParams({
    granularity: filters.granularity,
    group_by: filters.groupBy,
    category: filters.category,
    start: filters.start,
    end: filters.end,
  })
  return apiRequest<ReportResult>(`/reports/stats?${query}`, { method: "GET" })
}

export function createDetailExport(filters: ReportFilters, decrypted: boolean): Promise<ExportTask> {
  return apiRequest<ExportTask>("/reports/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filters: {
        start: `${filters.start}T00:00:00+08:00`,
        end: `${filters.end}T23:59:59+08:00`,
        category: filters.category === "all" ? null : filters.category,
      },
      decrypted,
    }),
  })
}

export function getExportTask(id: string): Promise<ExportTask> {
  return apiRequest<ExportTask>(`/reports/export/${id}`, { method: "GET" })
}

export function issueExportStepUp(id: string, password: string): Promise<{ token: string; expires_in: 300 }> {
  return apiRequest<{ token: string; expires_in: 300 }>(
    `/reports/export/${id}/step-up`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    },
    PASSWORD_AUTH_REQUEST_TIMEOUT_MS,
  )
}

export async function downloadExport(task: ExportTask, stepUpToken?: string): Promise<Blob> {
  if (!task.download_url) throw new ApiRequestError(0, "EXPORT_NOT_READY", "导出文件尚未就绪")
  // authorizedBlob 内部统一注入 Bearer，并让 Deadline 覆盖正文读取。
  const headers: Record<string, string> = {}
  if (stepUpToken) headers["X-Export-Step-Up"] = stepUpToken
  return authorizedBlob(task.download_url, { headers }, DOWNLOAD_TIMEOUT_MS)
}
