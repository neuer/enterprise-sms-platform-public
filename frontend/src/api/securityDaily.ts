import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { apiRequest } from "./client"
import type { NumberedPage } from "./pagination"
import type { components } from "./types.gen"

export type SecurityStatus = components["schemas"]["SecurityDailyReportModel"]["status"]
export type GenerationSource = components["schemas"]["SecurityDailyReportModel"]["generation_source"]
export type GenerationStatus = components["schemas"]["SecurityDailyReportModel"]["generation_status"]
export type DeliveryStatus = components["schemas"]["SecurityDailyReportModel"]["delivery_status"]
export type SecurityDailyConfigurationState = components["schemas"]["SecurityDailyOverviewModel"]["configuration_state"]

export type SecurityDailyOverview = components["schemas"]["SecurityDailyOverviewModel"]
export type SecurityDailyConfiguration = components["schemas"]["SecurityDailyConfigurationModel"]
export type SecurityDailyConfigurationUpdate = components["schemas"]["SecurityDailyConfigurationUpdateModel"]

// 生成契约将报告 payload 声明为泛型 object（additionalProperties，无字段结构），
// 以下 payload 子结构无对应 schema，按实际生成/消费结构保留手写。
export interface SecurityMetric {
  label: string
  value: string
  tone: "neutral" | "good" | "warn" | "danger"
  note: string
}

export interface SecurityDetailRow {
  label: string
  value: string
  assessment: string
  tone: "neutral" | "good" | "warn" | "danger"
}

export interface SecurityAuditRow {
  time: string
  actor: string
  source_ip: string
  action: string
  assessment: string
  tone: "neutral" | "good" | "warn" | "danger"
}

export interface SecurityActionItem {
  priority: "high" | "medium" | "low"
  title: string
  detail: string
}

export interface SecurityCoverageItem {
  source: string
  window: string
  status: string
  note: string
  tone: "neutral" | "good" | "warn" | "danger"
}

export interface SecurityDailyPayload {
  schema_version: 1
  report_date: string
  period_start: string
  period_end: string
  generated_at: string
  status: SecurityStatus
  summary: string
  pending_confirmation: string
  metrics: SecurityMetric[]
  ssh: SecurityDetailRow[]
  web: SecurityDetailRow[]
  audit: SecurityAuditRow[]
  runtime: SecurityDetailRow[]
  actions: SecurityActionItem[]
  coverage: SecurityCoverageItem[]
}

// 契约 SecurityDailyReportModel 的 payload/timeline 为宽松 object/object[] 且声明为可选；
// 前端保留必填的结构化 payload 与 timeline，其余字段直接绑定生成 schema。
export type SecurityDailyReport = Omit<components["schemas"]["SecurityDailyReportModel"], "payload" | "timeline"> & {
  payload: SecurityDailyPayload | null
  timeline: Array<{ type: string; at: string; label: string; detail?: string | null }>
}

// 分页壳保持 NumberedPage 封装（契约 SecurityDailyPageModel 的 items 引用宽松 report 模型）。
export type SecurityDailyPage = NumberedPage<SecurityDailyReport>

// 契约 SecurityDailyPreviewModel 的 payload 同样为宽松 object，保留结构化手写 payload。
export type SecurityDailyPreview = Omit<components["schemas"]["SecurityDailyPreviewModel"], "payload"> & {
  payload: SecurityDailyPayload | null
}

export type SecurityDailyDeliveryResponse = components["schemas"]["SecurityDailyDeliveryResponseModel"]

// 请求侧 camelCase 筛选参数仅用于查询串拼装，契约无对应 schema，保持手写。
export interface SecurityDailyFilters {
  dateFrom?: string
  dateTo?: string
  status?: SecurityStatus
  generationStatus?: GenerationStatus
  deliveryStatus?: DeliveryStatus
  page?: number
  pageSize?: number
}

const basePath = "/admin/security-daily"

export function getSecurityDailyOverview(signal?: AbortSignal): Promise<SecurityDailyOverview> {
  return apiRequest<SecurityDailyOverview>(`${basePath}/overview`, { method: "GET", signal })
}

export function getSecurityDailyConfiguration(signal?: AbortSignal): Promise<SecurityDailyConfiguration> {
  return apiRequest<SecurityDailyConfiguration>(`${basePath}/config`, { method: "GET", signal })
}

export function updateSecurityDailyConfiguration(
  configuration: SecurityDailyConfigurationUpdate,
): Promise<SecurityDailyConfiguration> {
  return apiRequest<SecurityDailyConfiguration>(`${basePath}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configuration),
  })
}

export function generateSecurityDailyReport(): Promise<SecurityDailyReport> {
  return apiRequest<SecurityDailyReport>(`${basePath}/generate`, {
    method: "POST",
  })
}

export function listSecurityDailyReports(
  filters: SecurityDailyFilters = {},
  signal?: AbortSignal,
): Promise<SecurityDailyPage> {
  const query = new URLSearchParams({
    page: String(filters.page ?? 1),
    page_size: String(filters.pageSize ?? DEFAULT_PAGE_SIZE),
  })
  if (filters.dateFrom) query.set("date_from", filters.dateFrom)
  if (filters.dateTo) query.set("date_to", filters.dateTo)
  if (filters.status) query.set("status", filters.status)
  if (filters.generationStatus) query.set("generation_status", filters.generationStatus)
  if (filters.deliveryStatus) query.set("delivery_status", filters.deliveryStatus)
  return apiRequest<SecurityDailyPage>(`${basePath}/reports?${query.toString()}`, { method: "GET", signal })
}

export function getSecurityDailyReport(reportId: number, signal?: AbortSignal): Promise<SecurityDailyReport> {
  return apiRequest<SecurityDailyReport>(`${basePath}/reports/${reportId}`, { method: "GET", signal })
}

export function previewSecurityDailyReport(reportId: number, signal?: AbortSignal): Promise<SecurityDailyPreview> {
  return apiRequest<SecurityDailyPreview>(`${basePath}/reports/${reportId}/preview`, { method: "GET", signal })
}

export function sendSecurityDailyReport(reportId: number): Promise<SecurityDailyDeliveryResponse> {
  return apiRequest<SecurityDailyDeliveryResponse>(`${basePath}/reports/${reportId}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  })
}

export function retrySecurityDailyReport(reportId: number): Promise<SecurityDailyDeliveryResponse> {
  return apiRequest<SecurityDailyDeliveryResponse>(`${basePath}/reports/${reportId}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  })
}
