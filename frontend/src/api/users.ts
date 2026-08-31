import { apiRequest } from "./client"
import type { UserRole } from "./auth"
export type UserProvider = "local" | "ad" | string
export type UserSyncStatus = "local" | "synced" | "pending" | "disabled"
export type CredentialStatus = "active" | "must_change" | null

export interface ManagedUser {
  account_id: number
  identity_id: number
  provider_code: UserProvider
  username: string
  display_name: string
  dept: string
  role: UserRole
  role_override: boolean
  status: 0 | 1
  identity_status: 0 | 1
  credential_status: CredentialStatus
  source_groups: string[]
  sync_status: UserSyncStatus
  last_synced_at: string | null
  last_login_at: string | null
}

export interface UserPage {
  items: ManagedUser[]
  total: number
  page: number
  page_size: number
}

export interface UserFilters {
  keyword: string
  providerCode: string | ""
  role: UserRole | ""
  status: 0 | 1 | ""
  page: number
  pageSize: number
}

export interface CreateLocalUserInput {
  username: string
  display_name: string
  dept: string
  role: UserRole
  temporary_password: string
}

export function listUsers(filters: UserFilters): Promise<UserPage> {
  const query = new URLSearchParams({
    page: String(filters.page),
    page_size: String(filters.pageSize),
  })
  if (filters.keyword.trim()) query.set("keyword", filters.keyword.trim())
  if (filters.providerCode) query.set("provider_code", filters.providerCode)
  if (filters.role) query.set("role", filters.role)
  if (filters.status !== "") query.set("status", String(filters.status))
  return apiRequest<UserPage>(`/admin/users?${query}`, { method: "GET" })
}

export function createLocalUser(payload: CreateLocalUserInput): Promise<ManagedUser> {
  return apiRequest<ManagedUser>("/admin/users/local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}

export function updateUserRole(
  accountId: number,
  role: UserRole,
  roleOverride: boolean,
): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/role`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, role_override: roleOverride }),
  })
}

export function updateUserStatus(accountId: number, status: 0 | 1): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  })
}

export function resetLocalPassword(
  accountId: number,
  temporaryPassword: string,
): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/admin/users/${accountId}/password/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temporary_password: temporaryPassword }),
  })
}

export function revokeUserSessions(accountId: number): Promise<void> {
  return apiRequest<void>(`/admin/users/${accountId}/sessions/revoke`, {
    method: "POST",
  })
}
