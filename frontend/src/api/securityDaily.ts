import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { apiRequest } from "./client"

export type SecurityStatus = "normal" | "attention" | "high"
export type GenerationSource = "auto" | "manual"
export type GenerationStatus = "pending" | "ready" | "failed" | "unavailable"
export type DeliveryStatus = "not_sent" | "pending" | "sending" | "sent" | "failed" | "unknown"
export type SecurityDailyConfigurationState = "disabled" | "dispatcher_missing" | "recipients_empty" | "ready"

export interface SecurityDailyOverview {
  enabled: boolean
  configuration_state: SecurityDailyConfigurationState
  schedule_time: string
  timezone: string
  period_description: string
  last_generated_at: string | null
  last_delivered_at: string | null
  next_scheduled_at: string | null
  latest_failure: string | null
  delivery_status: DeliveryStatus | null
  recipient_count: number
  resend_configured: boolean
  sender_domain: string
  sender_address: string
}

export interface SecurityDailyConfiguration {
  enabled: boolean
  recipients: string[]
  resend_api_key_configured: boolean
  sender_domain: string
  sender_address: string
}

export interface SecurityDailyConfigurationUpdate {
  enabled: boolean
  recipients: string[]
  resend_api_key?: string | null
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
  generation_source: GenerationSource
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
  state: "pending" | "sent" | "failed" | "unknown"
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

export function getSecurityDailyConfiguration(): Promise<SecurityDailyConfiguration> {
  return apiRequest<SecurityDailyConfiguration>(`${basePath}/config`, { method: "GET" })
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

export function listSecurityDailyReports(filters: SecurityDailyFilters = {}): Promise<SecurityDailyPage> {
  const query = new URLSearchParams({
    page: String(filters.page ?? 1),
    page_size: String(filters.pageSize ?? DEFAULT_PAGE_SIZE),
  })
  if (filters.dateFrom) query.set("date_from", filters.dateFrom)
  if (filters.dateTo) query.set("date_to", filters.dateTo)
  if (filters.status) query.set("status", filters.status)
  if (filters.generationStatus) query.set("generation_status", filters.generationStatus)
  if (filters.deliveryStatus) query.set("delivery_status", filters.deliveryStatus)
  return apiRequest<SecurityDailyPage>(`${basePath}/reports?${query.toString()}`, { method: "GET" })
}

export function getSecurityDailyReport(reportId: number): Promise<SecurityDailyReport> {
  return apiRequest<SecurityDailyReport>(`${basePath}/reports/${reportId}`, { method: "GET" })
}

export function previewSecurityDailyReport(reportId: number): Promise<SecurityDailyPreview> {
  return apiRequest<SecurityDailyPreview>(`${basePath}/reports/${reportId}/preview`, { method: "GET" })
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
