import { apiRequest } from "./webMessages"

export type SecurityStatus = "normal" | "attention" | "high"
export type GenerationStatus = "pending" | "ready" | "failed" | "unavailable"
export type DeliveryStatus = "not_sent" | "pending" | "sending" | "sent" | "failed"

export interface SecurityDailyOverview {
  enabled: boolean
  schedule_time: string
  timezone: string
  period_description: string
  last_generated_at: string | null
  last_delivered_at: string | null
  next_scheduled_at: string
  latest_failure: string | null
  delivery_status: DeliveryStatus | null
  recipient_count: number
  resend_configured: boolean
  sender_domain: string
  sender_address: string
  beat_restart_required: boolean
}

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

export interface SecurityDailyReport {
  id: number
  report_date: string
  period_start: string
  period_end: string
  status: SecurityStatus
  generation_status: GenerationStatus
  delivery_status: DeliveryStatus
  generated_at: string | null
  delivered_at: string | null
  recipient_count: number
  retry_count: number
  last_error: string | null
  last_error_at: string | null
  updated_at: string
  payload: SecurityDailyPayload | null
  timeline: Array<{ type: string; at: string; label: string; detail?: string | null }>
}

export interface SecurityDailyPage {
  items: SecurityDailyReport[]
  total: number
  page: number
  page_size: number
}

export interface SecurityDailyPreview {
  report_date: string
  status: SecurityStatus
  available: boolean
  message: string | null
  html: string
  text: string
  payload: SecurityDailyPayload | null
}

export interface SecurityDailyDeliveryResponse {
  request_id: string
  report_date: string
  action: "send" | "retry"
  state: "pending" | "sent" | "failed"
  idempotent: boolean
}

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

export function getSecurityDailyOverview(): Promise<SecurityDailyOverview> {
  return apiRequest<SecurityDailyOverview>(`${basePath}/overview`, { method: "GET" })
}

export function listSecurityDailyReports(filters: SecurityDailyFilters = {}): Promise<SecurityDailyPage> {
  const query = new URLSearchParams({
    page: String(filters.page ?? 1),
    page_size: String(filters.pageSize ?? 20),
  })
  if (filters.dateFrom) query.set("date_from", filters.dateFrom)
  if (filters.dateTo) query.set("date_to", filters.dateTo)
  if (filters.status) query.set("status", filters.status)
  if (filters.generationStatus) query.set("generation_status", filters.generationStatus)
  if (filters.deliveryStatus) query.set("delivery_status", filters.deliveryStatus)
  return apiRequest<SecurityDailyPage>(`${basePath}/reports?${query.toString()}`, { method: "GET" })
}

export function getSecurityDailyReport(reportDate: string): Promise<SecurityDailyReport> {
  return apiRequest<SecurityDailyReport>(`${basePath}/reports/${reportDate}`, { method: "GET" })
}

export function previewSecurityDailyReport(reportDate: string): Promise<SecurityDailyPreview> {
  return apiRequest<SecurityDailyPreview>(`${basePath}/reports/${reportDate}/preview`, { method: "GET" })
}

export function sendSecurityDailyReport(reportDate: string): Promise<SecurityDailyDeliveryResponse> {
  return apiRequest<SecurityDailyDeliveryResponse>(`${basePath}/reports/${reportDate}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  })
}

export function retrySecurityDailyReport(reportDate: string): Promise<SecurityDailyDeliveryResponse> {
  return apiRequest<SecurityDailyDeliveryResponse>(`${basePath}/reports/${reportDate}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  })
}
