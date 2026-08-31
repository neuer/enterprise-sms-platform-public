import {
  beginRefreshTabBinding,
  clearRefreshTabBinding,
  getRefreshTabBinding,
} from "./sessionTokens"

/**
 * 本模块是 pre-auth 流程（登录/刷新/改密/注销），刻意不走 client.ts 的
 * authorizedFetch：刷新处理器本身依赖 refreshRequest，套娃会造成递归。
 * 因此这里保留裸 fetch，但统一 30s 超时与带 code 的 AuthApiError。
 */
const AUTH_REQUEST_TIMEOUT_MS = 30_000

export type UserRole = "admin" | "approver" | "operator" | "viewer"

export interface AuthProvider {
  code: string
  name: string
  auth_flow: "password" | "redirect"
}

export interface PasswordPolicy {
  min_length: number
  max_length: number
  required_character_classes: number
  forbid_username: boolean
  description: string
}

export interface PlatformUser {
  account_id: number
  identity_id: number
  provider_code: string
  username: string
  display_name: string
  dept: string
  role: UserRole
}

export interface LoginSuccess {
  token: string
  expires_in: 900
  refresh_expires_in: number
  user: PlatformUser
}

export interface PasswordChangeRequired {
  change_token: string
  expires_in: 600
  next_action: "change_password"
}

export type LoginResponse = LoginSuccess | PasswordChangeRequired

interface ApiErrorBody {
  code?: string
  message?: string
}

export class AuthApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = "AuthApiError"
  }
}

function withTimeout(signal?: AbortSignal): AbortSignal {
  return signal ?? AbortSignal.timeout(AUTH_REQUEST_TIMEOUT_MS)
}

async function apiError(response: Response): Promise<AuthApiError> {
  const body = (await response.json().catch(() => ({}))) as ApiErrorBody
  return new AuthApiError(
    response.status,
    body.code || `HTTP_${response.status}`,
    body.message || body.code || `请求失败（${response.status}）`,
  )
}

export async function providerRequest(): Promise<AuthProvider[]> {
  const response = await fetch("/api/v1/web/auth/providers", {
    headers: { Accept: "application/json" },
    signal: withTimeout(),
  })
  if (!response.ok) throw await apiError(response)
  return (await response.json()) as AuthProvider[]
}

export async function passwordPolicyRequest(): Promise<PasswordPolicy> {
  const response = await fetch("/api/v1/web/auth/password-policy", {
    headers: { Accept: "application/json" },
    signal: withTimeout(),
  })
  if (!response.ok) throw await apiError(response)
  return (await response.json()) as PasswordPolicy
}

export async function loginRequest(
  providerCode: string,
  username: string,
  password: string,
  signal?: AbortSignal,
): Promise<LoginResponse> {
  const tabId = beginRefreshTabBinding()
  try {
    const response = await fetch("/api/v1/web/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_code: providerCode, username, password, tab_id: tabId }),
      signal: withTimeout(signal),
    })
    if (!response.ok) throw await apiError(response)
    return (await response.json()) as LoginResponse
  } catch (error) {
    clearRefreshTabBinding()
    throw error
  }
}

export async function refreshRequest(signal?: AbortSignal): Promise<LoginSuccess> {
  const tabId = getRefreshTabBinding()
  if (!tabId) {
    throw new AuthApiError(401, "UNAUTHORIZED", "当前标签页会话绑定缺失，请重新登录")
  }
  const response = await fetch("/api/v1/web/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tabId }),
    signal: withTimeout(signal),
  })
  if (!response.ok) throw await apiError(response)
  return (await response.json()) as LoginSuccess
}

export async function initialPasswordChangeRequest(
  changeToken: string,
  newPassword: string,
): Promise<void> {
  const response = await fetch("/api/v1/web/auth/password/initial", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ change_token: changeToken, new_password: newPassword }),
    signal: withTimeout(),
  })
  if (!response.ok) throw await apiError(response)
}

export async function passwordChangeRequest(
  token: string,
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const response = await fetch("/api/v1/web/auth/password/change", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
    signal: withTimeout(),
  })
  if (!response.ok) throw await apiError(response)
}

export async function logoutRequest(token: string, signal?: AbortSignal): Promise<void> {
  const response = await fetch("/api/v1/web/auth/logout", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    signal: withTimeout(signal),
  })
  if (!response.ok) throw await apiError(response)
}
