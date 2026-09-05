import { AUTH_JSON_MAX_BYTES, HttpBodyError, fetchJsonWithDeadline } from "./httpDeadline"
import { beginRefreshTabBinding, clearRefreshTabBinding, getRefreshTabBinding } from "./sessionTokens"

/**
 * 本模块是 pre-auth 流程（登录/刷新/改密/注销），刻意不走 client.ts 的
 * authorizedFetch：刷新处理器本身依赖 refreshRequest，套娃会造成递归。
 * 因此这里保留独立请求，但与业务层共用端到端 JSON Deadline。
 */
const AUTH_REQUEST_TIMEOUT_MS = 30_000
export const PASSWORD_AUTH_REQUEST_TIMEOUT_MS = 55_000
const REFRESH_REQUEST_TIMEOUT_MS = 10_000

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
  expires_in: number
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

async function authJson<T>(
  input: string,
  init: RequestInit = {},
  timeoutMs = AUTH_REQUEST_TIMEOUT_MS,
  callerSignal?: AbortSignal,
): Promise<T | null> {
  try {
    const { response, body } = await fetchJsonWithDeadline<T | ApiErrorBody>(input, init, {
      timeoutMs,
      callerSignal,
      maxBodyBytes: AUTH_JSON_MAX_BYTES,
      timeoutMessage: "认证请求超时",
      credentials: "same-origin",
    })
    if (!response.ok) {
      const errorBody = (body ?? {}) as ApiErrorBody
      throw new AuthApiError(
        response.status,
        errorBody.code || `HTTP_${response.status}`,
        errorBody.message || errorBody.code || `请求失败（${response.status}）`,
      )
    }
    return body as T | null
  } catch (error) {
    if (error instanceof HttpBodyError) {
      throw new AuthApiError(0, error.code, error.message)
    }
    throw error
  }
}

function requireJson<T>(body: T | null): T {
  if (body == null) {
    throw new AuthApiError(0, "INVALID_JSON_RESPONSE", "响应不是有效 JSON")
  }
  return body
}

export async function providerRequest(): Promise<AuthProvider[]> {
  return requireJson(
    await authJson<AuthProvider[]>("/api/v1/web/auth/providers", {
      headers: { Accept: "application/json" },
    }),
  )
}

export async function passwordPolicyRequest(): Promise<PasswordPolicy> {
  return requireJson(
    await authJson<PasswordPolicy>("/api/v1/web/auth/password-policy", {
      headers: { Accept: "application/json" },
    }),
  )
}

export async function loginRequest(
  providerCode: string,
  username: string,
  password: string,
  signal?: AbortSignal,
): Promise<LoginResponse> {
  const tabId = beginRefreshTabBinding()
  try {
    return requireJson(
      await authJson<LoginResponse>(
        "/api/v1/web/auth/login",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider_code: providerCode, username, password, tab_id: tabId }),
        },
        PASSWORD_AUTH_REQUEST_TIMEOUT_MS,
        signal,
      ),
    )
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
  return requireJson(
    await authJson<LoginSuccess>(
      "/api/v1/web/auth/refresh",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tab_id: tabId }),
      },
      REFRESH_REQUEST_TIMEOUT_MS,
      signal,
    ),
  )
}

export async function initialPasswordChangeRequest(changeToken: string, newPassword: string): Promise<void> {
  await authJson("/api/v1/web/auth/password/initial", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ change_token: changeToken, new_password: newPassword }),
  })
}

export async function passwordChangeRequest(
  token: string,
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await authJson("/api/v1/web/auth/password/change", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  })
}

export async function logoutRequest(token: string, signal?: AbortSignal): Promise<void> {
  await authJson(
    "/api/v1/web/auth/logout",
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    },
    AUTH_REQUEST_TIMEOUT_MS,
    signal,
  )
}
