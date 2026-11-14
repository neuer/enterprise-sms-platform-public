import { adminStepUpHeaders } from "./adminStepUp"
import type { UserRole } from "./auth"
import { apiRequest } from "./client"
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

export function updateConfigs(items: ConfigUpdate[], token: string): Promise<ConfigItem[]> {
  return apiRequest<ConfigItem[]>("/admin/configs", {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
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
