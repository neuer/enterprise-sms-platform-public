import { adminStepUpHeaders } from "./adminStepUp"
import { PASSWORD_AUTH_REQUEST_TIMEOUT_MS, type UserRole } from "./auth"
import type { VendorCredentialEnvelope, VendorSealSession } from "../lib/vendorSeal"
import {
  type ApiErrorBody,
  ApiRequestError,
  apiRequest,
  assertAuthorizedResultCurrent,
  authorizedJsonResult,
} from "./client"
import type { BillingPreview } from "./webMessages"
import type { NumberedPage } from "./pagination"
import type { components } from "./types.gen"

export type AuditItem = components["schemas"]["AuditModel"]

// 与 api/pagination.ts 的 NumberedPage<AuditItem> 同形（items/total/page/page_size），别名引用单点。
export type AuditPage = NumberedPage<AuditItem>

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
  /** 从结果排除指定动作（服务端等值排除）；空串不排除。 */
  excludeAction?: string
}

export type ConfigItem = components["schemas"]["ConfigModel"]

export type ConfigUpdate = components["schemas"]["ConfigUpdateModel"]

export type AdminUserRole = UserRole

export type LdapProviderConfig = components["schemas"]["LdapProviderConfig"]

// 保留手写：生成契约 AuthProviderAdmin 的 draft_config/active_config 是
// { [key: string]: unknown } 白名单字典（服务端按认证源动态裁剪字段）；本页仅管理
// AD/LDAP 认证源，表单 hydrate（Object.assign(adForm, draft_config)）与草稿编辑依赖
// LdapProviderConfig 的精确字段类型。其余字段与生成 schema 逐字段一致。
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

export type AuthProviderTestResult = components["schemas"]["AuthProviderTestResult"]

export type ExternalRoleMapping = components["schemas"]["AuthProviderRoleMapping"]

export type RoleMappings = components["schemas"]["AuthProviderRoleMappings"]

export type ExternalRoleMappingUpdate = components["schemas"]["AuthProviderRoleMappingUpdate"]

export function listAudits(filters: AuditFilters, signal?: AbortSignal): Promise<AuditPage> {
  const query = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  })
  if (filters.actor.trim()) query.set("actor", filters.actor.trim())
  if (filters.actorAccountId.trim()) query.set("actor_account_id", filters.actorAccountId.trim())
  if (filters.action.trim()) query.set("action", filters.action.trim())
  if (filters.excludeAction?.trim()) query.set("exclude_action", filters.excludeAction.trim())
  if (filters.objectType.trim()) query.set("object_type", filters.objectType.trim())
  if (filters.objectId.trim()) query.set("object_id", filters.objectId.trim())
  if (filters.correlationId.trim()) query.set("correlation_id", filters.correlationId.trim())
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<AuditPage>(`/admin/audit-logs?${query}`, { method: "GET", signal })
}

export const listAuditActions = (signal?: AbortSignal) =>
  apiRequest<string[]>("/admin/audit-logs/actions", { method: "GET", signal })

export const listConfigs = (signal?: AbortSignal) =>
  apiRequest<ConfigItem[]>("/admin/configs", { method: "GET", signal })

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

export function getAuthProvider(providerCode: string, signal?: AbortSignal): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode), { method: "GET", signal })
}

export function saveAuthProviderDraft(
  providerCode: string,
  config: LdapProviderConfig,
  token?: string,
): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/draft"), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify({ config }),
  })
}

export function testAuthProvider(providerCode: string): Promise<AuthProviderTestResult> {
  return apiRequest<AuthProviderTestResult>(providerPath(providerCode, "/test"), {
    method: "POST",
  })
}

export function activateAuthProvider(providerCode: string, token?: string): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/activate"), {
    method: "POST",
    headers: adminStepUpHeaders(token),
  })
}

export function disableAuthProvider(providerCode: string, token?: string): Promise<AuthProviderAdmin> {
  return apiRequest<AuthProviderAdmin>(providerPath(providerCode, "/disable"), {
    method: "POST",
    headers: adminStepUpHeaders(token),
  })
}

export function listAuthProviderRoleMappings(providerCode: string, signal?: AbortSignal): Promise<RoleMappings> {
  return apiRequest<RoleMappings>(providerPath(providerCode, "/role-mappings"), {
    method: "GET",
    signal,
  })
}

export function replaceAuthProviderRoleMappings(
  providerCode: string,
  mappings: ExternalRoleMappingUpdate[],
  expectedRevision: string,
  token?: string,
): Promise<RoleMappings> {
  return apiRequest<RoleMappings>(providerPath(providerCode, "/role-mappings"), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify({ mappings, expected_revision: expectedRevision }),
  })
}

export type VendorTestMode = components["schemas"]["VendorTestStatusModel"]["mode"]
export type VendorPauseKind = components["schemas"]["VendorTestStatusModel"]["pause_kind"]
export type VendorOperationStatus = components["schemas"]["VendorTestOperationModel"]["status"]
export type VendorOperationType = components["schemas"]["VendorTestOperationModel"]["operation_type"]

export type VendorTestStatus = components["schemas"]["VendorTestStatusModel"]

// 保留手写：生成契约 VendorTestOperationModel 把 safe_code/vendor_code/batch_no/checkpoint_id
// 标为可缺省，但后端 response_model 始终序列化这四个字段（缺省为 null），且 VendorTestConsole
// 以 `!== null` 判定"有无厂商错误码"（undefined !== null 恒为 true，与 null 语义不同），
// 故维持"键必存在、值可空"的精确类型；其余字段与生成 schema 逐字段一致。
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

export type VendorTestRecipient = components["schemas"]["RecipientModel"]

export type VendorStepUpResponse = components["schemas"]["StepUpResponseModel"]

export type VendorStepUpOperation = components["schemas"]["StepUpRequestModel"]["operation"]

export type VendorTestUatPayload = components["schemas"]["UatMessageRequestModel"]

export type VendorTestUatPreviewPayload = components["schemas"]["UatPreviewRequestModel"]

/**
 * vendor-test 薄封装：错误归一化复用 client.ts 的 ApiRequestError 回退链，
 * 额外保留真实联调特有的 Cache-Control: no-store 断言与空 body 拒绝。
 */
async function vendorRequest<T>(path: string, init: RequestInit, timeoutMs?: number): Promise<T> {
  const result = await authorizedJsonResult<T>(`/api/v1/web/admin/vendor-test${path}`, init, timeoutMs)
  assertAuthorizedResultCurrent(result)
  if (!result.ok) {
    const body = (result.body ?? {}) as ApiErrorBody
    throw new ApiRequestError(
      result.status,
      body.code || `HTTP_${result.status}`,
      body.message || body.code || `请求失败（${result.status}）`,
      body.detail,
    )
  }
  const cacheControl = result.headers.get("cache-control")?.toLowerCase() || ""
  if (!cacheControl.split(",").some((value) => value.trim() === "no-store")) {
    throw new Error("真实联调响应缓存策略无效")
  }
  if (result.body == null) {
    throw new ApiRequestError(result.status, "INVALID_JSON_RESPONSE", "响应不是有效 JSON")
  }
  return result.body as T
}

function jsonRequest(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }
}

export function getVendorTestStatus(signal?: AbortSignal): Promise<VendorTestStatus> {
  return vendorRequest<VendorTestStatus>("/status", { method: "GET", signal })
}

export function issueVendorTestStepUp(
  operation: VendorStepUpOperation,
  password: string,
): Promise<VendorStepUpResponse> {
  return vendorRequest<VendorStepUpResponse>(
    "/step-up",
    jsonRequest("POST", { operation, password }),
    PASSWORD_AUTH_REQUEST_TIMEOUT_MS,
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

export function listVendorTestRecipients(signal?: AbortSignal): Promise<VendorTestRecipient[]> {
  return vendorRequest<VendorTestRecipient[]>("/recipients", { method: "GET", signal })
}

export function addVendorTestRecipient(label: string, phone: string): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>("/recipients", jsonRequest("POST", { label, phone }))
}

export function disableVendorTestRecipient(id: number): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>(`/recipients/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}

export function refreshVendorTestRecipientIndex(id: number, phone: string): Promise<VendorTestRecipient> {
  return vendorRequest<VendorTestRecipient>(
    `/recipients/${encodeURIComponent(id)}/refresh-index`,
    jsonRequest("POST", { phone }),
  )
}

export function activateVendorTest(stepUpToken: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>("/activate", jsonRequest("POST", { step_up_token: stepUpToken }))
}

export function resetVendorTest(stepUpToken: string): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>("/reset", jsonRequest("POST", { step_up_token: stepUpToken }))
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

export function getVendorTestOperation(operationId: string, signal?: AbortSignal): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(`/operations/${encodeURIComponent(operationId)}`, {
    method: "GET",
    signal,
  })
}

export function sendVendorTestUat(payload: VendorTestUatPayload): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>("/messages", jsonRequest("POST", payload))
}

export function previewVendorTestUat(payload: VendorTestUatPreviewPayload): Promise<BillingPreview> {
  return vendorRequest<BillingPreview>("/messages/preview", jsonRequest("POST", payload))
}

export function getVendorTestUat(operationId: string, signal?: AbortSignal): Promise<VendorTestOperation> {
  return vendorRequest<VendorTestOperation>(`/messages/${encodeURIComponent(operationId)}`, {
    method: "GET",
    signal,
  })
}
