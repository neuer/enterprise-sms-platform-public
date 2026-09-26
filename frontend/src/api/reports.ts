import { nextShanghaiMidnight } from "../lib/time"
import { PASSWORD_AUTH_REQUEST_TIMEOUT_MS } from "./auth"
import { apiRequest, authorizedBlob, ApiRequestError, DOWNLOAD_TIMEOUT_MS } from "./client"
import type { MessageCategory } from "../lib/labels"
import type { components } from "./types.gen"

export type ReportGranularity = components["schemas"]["ReportingModel"]["granularity"]
export type ReportGroupBy = components["schemas"]["ReportingModel"]["group_by"]
// 历史别名：类别部分单点为 lib/labels 的 MessageCategory（"all" 为报表聚合选项），本别名由后续收敛移除。
// 与契约 ReportingModel.category 枚举（"verify"|"notice"|"market"|"all"）逐字面量一致。
export type ReportCategory = MessageCategory | "all"
/** 趋势堆叠可选指标：消息数或计费条。 */
export type ReportTrendMetric = components["schemas"]["ReportingModel"]["metric"]

export type ReportRow = components["schemas"]["ReportingRowModel"]

export type ReportSummary = components["schemas"]["ReportingSummaryModel"]

export type ReportDimSummary = components["schemas"]["ReportingDimSummaryModel"]

export type ReportTrend = components["schemas"]["ReportingTrendModel"]

export type ReportResult = components["schemas"]["ReportingModel"]

export type ExportTask = components["schemas"]["ExportTask"]

// 以下为请求侧 camelCase 筛选/分页/排序参数，仅用于查询串拼装，契约无对应 schema，保持手写。
export interface ReportFilters {
  granularity: ReportGranularity
  groupBy: ReportGroupBy
  category: ReportCategory
  start: string
  end: string
}

export type ReportSort = "period_start" | "total" | "total_segments" | "success_rate"

export interface ReportPageOptions {
  page?: number
  size?: number
  sort?: ReportSort
  order?: "asc" | "desc"
  metric?: ReportTrendMetric
}

export function getReport(
  filters: ReportFilters,
  options: ReportPageOptions = {},
  signal?: AbortSignal,
): Promise<ReportResult> {
  const query = new URLSearchParams({
    granularity: filters.granularity,
    group_by: filters.groupBy,
    category: filters.category,
    start: filters.start,
    end: filters.end,
    page: String(options.page ?? 1),
    size: String(options.size ?? 20),
    sort: options.sort ?? "period_start",
    order: options.order ?? "desc",
    metric: options.metric ?? "total",
  })
  return apiRequest<ReportResult>(`/reports/stats?${query}`, { method: "GET", signal })
}

export function createDetailExport(filters: ReportFilters, decrypted: boolean): Promise<ExportTask> {
  return apiRequest<ExportTask>("/reports/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filters: {
        start: `${filters.start}T00:00:00+08:00`,
        end_exclusive: nextShanghaiMidnight(filters.end),
        category: filters.category === "all" ? null : filters.category,
      },
      decrypted,
    }),
  })
}

export function getExportTask(id: string, signal?: AbortSignal): Promise<ExportTask> {
  return apiRequest<ExportTask>(`/reports/export/${id}`, { method: "GET", signal })
}

// 契约 ExportStepUpResponse.expires_in 为 number（@default 300），手写保留字面量 300（窄于契约，
// 属实质差异故不迁移；消费方仅读取 .token）。
export function issueExportStepUp(
  id: string,
  password: string,
  signal?: AbortSignal,
): Promise<{ token: string; expires_in: 300 }> {
  return apiRequest<{ token: string; expires_in: 300 }>(
    `/reports/export/${id}/step-up`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
      signal,
    },
    PASSWORD_AUTH_REQUEST_TIMEOUT_MS,
  )
}

export async function downloadExport(task: ExportTask, stepUpToken?: string, signal?: AbortSignal): Promise<Blob> {
  if (!task.download_url) throw new ApiRequestError(0, "EXPORT_NOT_READY", "导出文件尚未就绪")
  // authorizedBlob 内部统一注入 Bearer，并让 Deadline 覆盖正文读取。
  const headers: Record<string, string> = {}
  if (stepUpToken) headers["X-Export-Step-Up"] = stepUpToken
  return authorizedBlob(task.download_url, { headers, signal }, DOWNLOAD_TIMEOUT_MS)
}
