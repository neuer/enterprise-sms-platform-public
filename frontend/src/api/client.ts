/**
 * 前端请求基建单点：同源断言、超时、Bearer 注入、401+UNAUTHORIZED 单飞刷新重放、
 * 会话代际联动取消。所有业务 api 模块统一使用本文件的 apiRequest/authorizedFetch，
 * 禁止再平行实现请求封装（auth.ts 为 pre-auth 例外，见该文件注释）。
 */
import { AuthApiError, refreshRequest } from "./auth"
import { withRefreshLock } from "./refreshLock"
import {
  getSessionGeneration,
  invalidateSessionGeneration,
  isCurrentSessionGeneration,
  trackSessionController,
} from "./sessionGeneration"
import {
  clearAccessSession,
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  setAccessSession,
} from "./sessionTokens"

export interface ApiErrorBody {
  code?: string
  message?: string
  detail?: unknown
}

export class ApiRequestError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: unknown

  constructor(status: number, code: string, message: string, detail: unknown = null) {
    super(message)
    this.name = "ApiRequestError"
    this.status = status
    this.code = code
    this.detail = detail
  }
}

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
const REFRESH_TIMEOUT_MS = 10_000
export const DOWNLOAD_TIMEOUT_MS = 120_000
type RefreshResult = "refreshed" | "unauthorized" | "unavailable"
let refreshInFlight: Promise<RefreshResult> | null = null
const sessionControllers = new Set<AbortController>()
window.addEventListener("sms:session-clearing", () => {
  invalidateSessionGeneration()
  cancelSessionRequests()
})

export function authorization(): Record<string, string> {
  const token = getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function assertSameOrigin(url: string): void {
  let target: URL
  try {
    target = new URL(url, window.location.origin)
  } catch {
    throw new ApiRequestError(0, "INVALID_REQUEST_URL", "请求 URL 无效")
  }
  if (!["http:", "https:"].includes(target.protocol) || target.origin !== window.location.origin) {
    throw new ApiRequestError(0, "CROSS_ORIGIN_REQUEST", "授权请求必须与当前站点同源")
  }
}

function cancelSessionRequests(): void {
  for (const controller of sessionControllers) controller.abort()
  sessionControllers.clear()
}

function fetchWithTimeout(
  url: string,
  init: RequestInit,
  controller: AbortController,
  timeoutMs: number,
): Promise<Response> {
  const timer = window.setTimeout(
    () => controller.abort(new DOMException("请求超时", "TimeoutError")),
    timeoutMs,
  )
  return fetch(url, { ...init, signal: controller.signal }).finally(() => {
    window.clearTimeout(timer)
  })
}

function requestWithCurrentAuthorization(
  url: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<Response> {
  assertSameOrigin(url)
  const headers: Record<string, string> = {}
  if (init.headers instanceof Headers) {
    init.headers.forEach((value, key) => {
      headers[key] = value
    })
  } else if (Array.isArray(init.headers)) {
    for (const [key, value] of init.headers) headers[key] = value
  } else if (init.headers) {
    Object.assign(headers, init.headers)
  }
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === "authorization") delete headers[key]
  }
  const token = getAccessToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const controller = new AbortController()
  const externalSignal = init.signal
  const abortFromExternal = () => controller.abort(externalSignal?.reason)
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort(externalSignal.reason)
    else externalSignal.addEventListener("abort", abortFromExternal, { once: true })
  }
  sessionControllers.add(controller)
  const releaseTrack = trackSessionController(controller)
  return fetchWithTimeout(url, { ...init, headers }, controller, timeoutMs).finally(() => {
    sessionControllers.delete(controller)
    releaseTrack()
    if (externalSignal) externalSignal.removeEventListener("abort", abortFromExternal)
  })
}

function clearSession(): void {
  invalidateSessionGeneration()
  cancelSessionRequests()
  clearAccessSession()
  clearRefreshTabBinding()
  window.dispatchEvent(new Event("sms:unauthorized"))
}

async function refreshSession(): Promise<RefreshResult> {
  if (refreshInFlight) return refreshInFlight
  const epochAtRequest = getSessionGeneration()
  refreshInFlight = withRefreshLock(async () => {
    if (!isCurrentSessionGeneration(epochAtRequest)) {
      return "unauthorized"
    }
    const epoch = getSessionGeneration()
    try {
      const currentUser = getSessionUser()
      const controller = new AbortController()
      const releaseTrack = trackSessionController(controller)
      const timer = window.setTimeout(
        () => controller.abort(new DOMException("刷新超时", "TimeoutError")),
        REFRESH_TIMEOUT_MS,
      )
      let result: Awaited<ReturnType<typeof refreshRequest>>
      try {
        result = await refreshRequest(controller.signal)
      } finally {
        window.clearTimeout(timer)
        releaseTrack()
      }
      if (!isCurrentSessionGeneration(epoch)) {
        clearAccessSession()
        return "unauthorized"
      }
      if (
        currentUser &&
        Number.isInteger(currentUser.account_id) &&
        currentUser.account_id > 0 &&
        Number.isInteger(currentUser.identity_id) &&
        currentUser.identity_id > 0 &&
        (currentUser.account_id !== result.user.account_id ||
          currentUser.identity_id !== result.user.identity_id)
      ) {
        clearSession()
        return "unauthorized"
      }
      setAccessSession(result.token, result.user)
      window.dispatchEvent(new Event("sms:session-refreshed"))
      return "refreshed"
    } catch (error) {
      if (!isCurrentSessionGeneration(epoch)) {
        clearAccessSession()
        return "unauthorized"
      }
      if (error instanceof AuthApiError && error.status === 401) {
        clearSession()
        return "unauthorized"
      }
      return "unavailable"
    }
  })
  try {
    return await refreshInFlight
  } finally {
    refreshInFlight = null
  }
}

async function errorCode(response: Response): Promise<string | undefined> {
  try {
    return ((await response.clone().json()) as ApiErrorBody).code
  } catch {
    return undefined
  }
}

export async function authorizedFetch(
  url: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const attemptedToken = getAccessToken()
  const response = await requestWithCurrentAuthorization(url, init, timeoutMs)
  const code = await errorCode(response)
  if (response.status === 423 && code === "ACCOUNT_LOCKED") {
    clearSession()
    return response
  }
  if (response.status !== 401 || code !== "UNAUTHORIZED") return response

  const currentToken = getAccessToken()
  if (currentToken && attemptedToken && currentToken !== attemptedToken) {
    return requestWithCurrentAuthorization(url, init, timeoutMs)
  }
  const refreshed = await refreshSession()
  if (refreshed === "refreshed") {
    return requestWithCurrentAuthorization(url, init, timeoutMs)
  }
  if (refreshed === "unavailable") {
    return new Response(
      JSON.stringify({
        code: "AUTH_SESSION_UNAVAILABLE",
        message: "会话权威状态暂不可用，请稍后重试",
        detail: null,
      }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )
  }
  return response
}

async function unwrapJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody
    throw new ApiRequestError(
      response.status,
      body.code || `HTTP_${response.status}`,
      body.message || body.code || `请求失败（${response.status}）`,
      body.detail,
    )
  }
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T
  }
  return (await response.json()) as T
}

/** Web 业务端点：path 自动加 `/api/v1/web` 前缀。 */
export async function apiRequest<T>(path: string, init: RequestInit): Promise<T> {
  return unwrapJson<T>(await authorizedFetch(`/api/v1/web${path}`, init))
}

/** 绝对路径端点（如 `/api/v1/messages/...`）：与 apiRequest 同错误类型，不加前缀。 */
export async function apiRequestAbs<T>(path: string, init: RequestInit): Promise<T> {
  return unwrapJson<T>(await authorizedFetch(path, init))
}
