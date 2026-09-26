import { adminStepUpHeaders } from "./adminStepUp"
import { apiRequest } from "./client"
import type { NumberedPage } from "./pagination"
import type { UserRole } from "./auth"
import type { components } from "./types.gen"

/** 契约为自由 string；保留已知取值的字面量并集仅作文档（类型等价于 string）。 */
export type UserProvider = "local" | "ad" | string
export type UserSyncStatus = components["schemas"]["User"]["sync_status"]
export type CredentialStatus = components["schemas"]["User"]["credential_status"]

export type ManagedUser = components["schemas"]["User"]

export type UserPage = NumberedPage<ManagedUser>

export interface UserFilters {
  keyword: string
  providerCode: string | ""
  role: UserRole | ""
  status: 0 | 1 | ""
  page: number
  pageSize: number
}

export type CreateLocalUserInput = components["schemas"]["LocalUserCreate"]

export function listUsers(filters: UserFilters, signal?: AbortSignal): Promise<UserPage> {
  const query = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  })
  if (filters.keyword.trim()) query.set("keyword", filters.keyword.trim())
  if (filters.providerCode) query.set("provider_code", filters.providerCode)
  if (filters.role) query.set("role", filters.role)
  if (filters.status !== "") query.set("status", String(filters.status))
  return apiRequest<UserPage>(`/admin/users?${query}`, { method: "GET", signal })
}

export function createLocalUser(payload: CreateLocalUserInput, token?: string): Promise<ManagedUser> {
  return apiRequest<ManagedUser>("/admin/users/local", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify(payload),
  })
}

export function updateUserRole(
  accountId: number,
  role: UserRole,
  roleOverride: boolean,
  token?: string,
): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/role`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify({ role, role_override: roleOverride }),
  })
}

export function updateUserStatus(accountId: number, status: 0 | 1, token?: string): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify({ status }),
  })
}

export function resetLocalPassword(accountId: number, temporaryPassword: string, token?: string): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/password/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...adminStepUpHeaders(token) },
    body: JSON.stringify({ temporary_password: temporaryPassword }),
  })
}

export function revokeUserSessions(accountId: number): Promise<void> {
  return apiRequest<void>(`/admin/users/${accountId}/sessions/revoke`, {
    method: "POST",
  })
}
