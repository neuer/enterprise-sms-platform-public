import type { VendorCredentialEnvelope, VendorSealSession } from "../lib/vendorSeal"
import { apiRequest, authorizedFetch, type BillingPreview } from "./webMessages"

export interface AuditItem {
  id: number
  correlation_id: string
  actor: string
  actor_subject_kind: "human" | "api_app" | "system" | "legacy_unknown"
  actor_account_id: number | null
  actor_identity_id: number | null
  actor_app_id: number | null
  role: string | null
  ip: string | null
  action: string
  object_type: string | null
  object_id: string | null
  before_val: Record<string, unknown> | null
  after_val: Record<string, unknown> | null
  created_at: string
}

export interface AuditPage {
  items: AuditItem[]
  total: number
  page: number
  page_size: number
}

export interface AuditFilters {
  actor: string
  actorAccountId: string
  action: string
  objectType: string
  objectId: string
  correlationId: string
  start: string
  end: string
  page: number
  pageSize: number
}

export interface ConfigItem {
  key: string
  value: string | null
  value_type: "str" | "int" | "bool" | "json"
  description: string | null
  group: string
  sensitive: boolean
  configured: boolean
  beat_restart_required: boolean
  updated_by: string | null
  updated_at: string | null
  default: string
  min_value: number | null
  max_value: number | null
}

export interface ConfigUpdate {
  key: string
  value: string | null
}

export type AdminUserRole = "admin" | "approver" | "operator" | "viewer"

export interface LdapProviderConfig {
  server: string
  base_dn: string
  bind_dn: string
  user_search_filter: string
  username_attribute: string
  display_name_attribute: string
  dept_attribute: string
  subject_attribute: string
  group_attribute: string
  connect_timeout_s: number
  receive_timeout_s: number
}

export interface AuthProviderAdmin {
  code: string
  name: string
  kind: string
  enabled: boolean
  draft_config: LdapProviderConfig
  active_config: LdapProviderConfig | null
  draft_version: number
  tested_version: number | null
  active_version: number | null
  last_tested_at: string | null
  last_test_status: string | null
  bind_secret_available: boolean
  ca_available: boolean
}

export interface AuthProviderTestResult {
  success: boolean
  result_code: string
}

export interface ExternalRoleMapping {
  external_group: string
  role: AdminUserRole
}

export interface RoleMappings {
  mappings: ExternalRoleMapping[]
}

export function listAudits(filters: AuditFilters): Promise<AuditPage> {
  const query = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  })
  if (filters.actor.trim()) query.set("actor", filters.actor.trim())
  if (filters.actorAccountId.trim()) query.set("actor_account_id", filters.actorAccountId.trim())
  if (filters.action.trim()) query.set("action", filters.action.trim())
  if (filters.objectType.trim()) query.set("object_type", filters.objectType.trim())
  if (filters.objectId.trim()) query.set("object_id", filters.objectId.trim())
  if (filters.correlationId.trim()) query.set("correlation_id", filters.correlationId.trim())
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<AuditPage>(`/admin/audit-logs?${query}`, { method: "GET" })
}

export const listAuditActions = () =>
  apiRequest<string[]>("/admin/audit-logs/actions", { method: "GET" })

export const listConfigs = () =>
  apiRequest<ConfigItem[]>("/admin/configs", { method: "GET" })

export function updateConfigs(items: ConfigUpdate[]): Promise<ConfigItem[]> {
  return apiRequest<ConfigItem[]>("/admin/configs", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  })
}

function providerPath(providerCode: string, suffix = ""): string {
  return `/admin/auth-providers/${encodeURIComponent(providerCode)}${suffix}`
}

export function getAuthProvider(providerCode: string): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode), { method: "GET" })
}

export function saveAuthProviderDraft(
  providerCode: string,
  config: LdapProviderConfig,
): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/draft"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  })
}

export function testAuthProvider(providerCode: string): Promise<AuthProviderTestResult> {
  return apiRequest<AuthProviderTestResult>(providerPath(providerCode, "/test"), {
    method: "POST",
  })
}

export function activateAuthProvider(providerCode: string): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/activate"), {
    method: "POST",
  })
}

export function disableAuthProvider(providerCode: string): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/disable"), {
    method: "POST",
  })
}

export function listAuthProviderRoleMappings(providerCode: string): Promise<RoleMappings> {
  return apiRequest<RoleMappings>(providerPath(providerCode, "/role-mappings"), {
    method: "GET",
  })
}

export function replaceAuthProviderRoleMappings(
  providerCode: string,
  mappings: ExternalRoleMapping[],
): Promise<RoleMappings> {
  return apiRequest<RoleMappings>(providerPath(providerCode, "/role-mappings"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mappings }),
  })
}

export type VendorTestMode = "setup_required" | "inactive" | "controlled" | "blocked"
export type VendorPauseKind = "manual" | "critical" | "daily" | null
export type VendorOperationStatus = "requested" | "running" | "succeeded" | "failed"
export type VendorOperationType =
  | "install_credentials"
  | "rotate_credentials"
  | "activate"
  | "pause"
  | "resume"
  | "reset_configuration"
  | "uat_send"

export interface VendorTestStatus {
  mode: VendorTestMode
  heartbeat_at: string
  credential_configured: boolean
  active_recipient_count: number
  pause_kind: VendorPauseKind
  daily_limit: 100
}

export interface VendorTestOperation {
  operation_id: string
  operation_type: VendorOperationType
  status: VendorOperationStatus
  safe_code: string | null
  vendor_code: number | null
  batch_no: string | null
  checkpoint_id: string | null
  requested_at: string
  completed_at: string | null
}

export interface VendorTestRecipient {
  id: number
  label: string
  phone_mask: string
  status: "active" | "disabled"
  created_at: string
  disabled_at: string | null
}

export interface VendorStepUpResponse {
  token: string
  expires_in: 300
}

export type VendorStepUpOperation =
  | "install_credentials"
  | "rotate_credentials"
  | "activate"
  | "reset_configuration"
  | "resume_critical"

export interface VendorTestUatPayload {
  recipient_id: number
  app_id: number
  biz_id: string
  category: "verify" | "notice" | "market"
  content?: string
  template_id?: number
  template_params?: string[]
  sign_name?: string
  consent_confirmed: boolean
  remark?: string
}

export type VendorTestUatPreviewPayload = Omit<
  VendorTestUatPayload,
  "recipient_id" | "remark" | "biz_id"
>

interface VendorApiErrorBody {
  code?: string
  message?: string
}

export class VendorRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | undefined,
    message: string,
  ) {
    super(message)
    this.name = "VendorRequestError"
  }
}

async function vendorRequest<T>(path: string, init: RequestInit): Promise<T> {
  const response = await authorizedFetch(`/api/v1/web/admin/vendor-test${path}`, init)
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as VendorApiErrorBody
    throw new VendorRequestError(
      response.status,
      body.code,
      body.message || body.code || `请求失败（${response.status}）`,
    )
  }
  const cacheControl = response.headers.get("cache-control")?.toLowerCase() || ""
  if (!cacheControl.split(",").some((value) => value.trim() === "no-store")) {
    throw new Error("真实联调响应缓存策略无效")
  }
  return (await response.json()) as T
}

function jsonRequest(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }
}

export function getVendorTestStatus(): Promise<VendorTestStatus> {
  return vendorRequest<VendorTestStatus>("/status", { method: "GET" })
}

export function issueVendorTestStepUp(
  operation: VendorStepUpOperation,
  password: string,
): Promise<VendorStepUpResponse> {
  return vendorRequest<VendorStepUpResponse>(
    "/step-up",
    jsonRequest("POST", { operation, password }),
  )
}

export function createVendorSealSession(
  operation: "install_credentials" | "rotate_credentials",
): Promise<VendorSealSession> {
  return vendorRequest<VendorSealSession>("/seal-sessions", jsonRequest("POST", { operation }))
}

export function installVendorCredentials(
  operation: "install_credentials" | "rotate_credentials",
  stepUpToken: string,
  envelope: VendorCredentialEnvelope,
): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    "/credentials",
    jsonRequest("PUT", { operation, step_up_token: stepUpToken, ...envelope }),
  )
}

export function listVendorTestRecipients(): Promise<VendorTestRecipient[]> {
  return vendorRequest<VendorTestRecipient[]>("/recipients", { method: "GET" })
}

export function addVendorTestRecipient(
  label: string,
  phone: string,
): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>(
    "/recipients",
    jsonRequest("POST", { label, phone }),
  )
}

export function disableVendorTestRecipient(id: number): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>(`/recipients/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}

export function refreshVendorTestRecipientIndex(
  id: number,
  phone: string,
): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>(
    `/recipients/${encodeURIComponent(id)}/refresh-index`,
    jsonRequest("POST", { phone }),
  )
}

export function activateVendorTest(stepUpToken: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    "/activate",
    jsonRequest("POST", { step_up_token: stepUpToken }),
  )
}

export function resetVendorTest(stepUpToken: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    "/reset",
    jsonRequest("POST", { step_up_token: stepUpToken }),
  )
}

export function pauseVendorTest(): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>("/pause", jsonRequest("POST", {}))
}

export function resumeVendorTest(stepUpToken?: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    "/resume",
    jsonRequest("POST", stepUpToken ? { step_up_token: stepUpToken } : {}),
  )
}

export function getVendorTestOperation(operationId: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    `/operations/${encodeURIComponent(operationId)}`,
    { method: "GET" },
  )
}

export function sendVendorTestUat(
  payload: VendorTestUatPayload,
): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>("/messages", jsonRequest("POST", payload))
}

export function previewVendorTestUat(
  payload: VendorTestUatPreviewPayload,
): Promise<BillingPreview> {
  return vendorRequest<BillingPreview>("/messages/preview", jsonRequest("POST", payload))
}

export function getVendorTestUat(operationId: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(
    `/messages/${encodeURIComponent(operationId)}`,
    { method: "GET" },
  )
}
