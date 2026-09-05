/**
 * 前端请求基建单点：同源断言、端到端 Deadline、Bearer 注入、
 * 401+UNAUTHORIZED 单飞刷新重放、会话代际联动取消。所有业务 api 模块统一使用
 * 本文件的 apiRequest/authorizedFetch/authorizedBlob，禁止再平行实现请求封装
 * （auth.ts 为 pre-auth 例外，见该文件注释）。
 */
import { AuthApiError, refreshRequest } from "./auth"
import {
  API_JSON_MAX_BYTES,
  DOWNLOAD_MAX_BYTES,
  HttpBodyError,
  createDeadline,
  joinAbortSignals,
  readJsonBody,
  readLimitedBlob,
} from "./httpDeadline"
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
export const DOWNLOAD_TIMEOUT_MS = 120_000

export interface AuthorizedJsonResult<T> {
  status: number
  ok: boolean
  headers: Headers
  body: T | ApiErrorBody | null
}

type RefreshResult = "refreshed" | "unauthorized" | "reauth-required" | "unavailable"
type AuthDecision = "account-locked" | "reauth-required" | "context-changed" | "unauthorized" | "none"
let refreshInFlight: Promise<RefreshResult> | null = null
const sessionControllers = new Set<AbortController>()
window.addEventListener("sms:session-clearing", () => {
  invalidateSessionGeneration()
  cancelSessionRequests()
  clearAccessSession()
  clearRefreshTabBinding()
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

function mapHttpBodyError(error: unknown): never {
  if (error instanceof HttpBodyError) {
    throw new ApiRequestError(0, error.code, error.message)
  }
  throw error
}

function startAuthorizedAttempt(
  timeoutMs: number,
  externalSignal: AbortSignal | undefined,
  timeoutMessage: string,
): { signal: AbortSignal; cleanup: () => void } {
  const sessionController = new AbortController()
  sessionControllers.add(sessionController)
  const releaseTrack = trackSessionController(sessionController)
  const joined = externalSignal
    ? joinAbortSignals([sessionController.signal, externalSignal])
    : { signal: sessionController.signal, cleanup: () => undefined }
  const deadline = createDeadline(timeoutMs, {
    callerSignal: joined.signal,
    timeoutMessage,
  })
  return {
    signal: deadline.signal,
    cleanup: () => {
      deadline.cleanup()
      joined.cleanup()
      sessionControllers.delete(sessionController)
      releaseTrack()
    },
  }
}

function authorizedHeaders(init: RequestInit): Record<string, string> {
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
  return headers
}

async function fetchAuthorizedOnce(url: string, init: RequestInit, signal: AbortSignal): Promise<Response> {
  assertSameOrigin(url)
  return fetch(url, { ...init, headers: authorizedHeaders(init), signal })
}

function rebuildJsonResponse(response: Response, body: unknown): Response {
  const headers = new Headers(response.headers)
  headers.delete("content-length")
  if (!headers.has("content-type")) headers.set("Content-Type", "application/json")
  return new Response(body == null ? null : JSON.stringify(body), {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

function classifyAuthDecision(status: number, body: unknown): AuthDecision {
  const code =
    body && typeof body === "object" && "code" in body && typeof body.code === "string" ? body.code : undefined
  if (status === 423 && code === "ACCOUNT_LOCKED") return "account-locked"
  if (status === 401 && code === "AUTH_REAUTH_REQUIRED") return "reauth-required"
  if (status === 409 && code === "AUTH_CONTEXT_CHANGED") return "context-changed"
  if (status === 401 && code === "UNAUTHORIZED") return "unauthorized"
  return "none"
}

function applyAuthDecision(decision: AuthDecision): void {
  if (decision === "account-locked" || decision === "context-changed") {
    clearSession()
  } else if (decision === "reauth-required") {
    clearSession("reauth-required")
  }
}

function clearSession(broadcast: "unauthorized" | "reauth-required" | "none" = "unauthorized"): void {
  invalidateSessionGeneration()
  cancelSessionRequests()
  clearAccessSession()
  clearRefreshTabBinding()
  if (broadcast === "unauthorized") {
    window.dispatchEvent(new Event("sms:unauthorized"))
  } else if (broadcast === "reauth-required") {
    window.dispatchEvent(new Event("sms:reauth-required"))
  }
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
      let result: Awaited<ReturnType<typeof refreshRequest>>
      try {
        result = await refreshRequest(controller.signal)
      } finally {
        releaseTrack()
      }
      if (!isCurrentSessionGeneration(epoch)) {
        return "unauthorized"
      }
      if (
        currentUser &&
        Number.isInteger(currentUser.account_id) &&
        currentUser.account_id > 0 &&
        Number.isInteger(currentUser.identity_id) &&
        currentUser.identity_id > 0 &&
        (currentUser.account_id !== result.user.account_id || currentUser.identity_id !== result.user.identity_id)
      ) {
        clearSession()
        return "unauthorized"
      }
      setAccessSession(result.token, result.user)
      window.dispatchEvent(new Event("sms:session-refreshed"))
      return "refreshed"
    } catch (error) {
      if (!isCurrentSessionGeneration(epoch)) {
        return "unauthorized"
      }
      if (error instanceof AuthApiError && error.status === 401) {
        if (error.code === "AUTH_REAUTH_REQUIRED") {
          clearSession("reauth-required")
          return "reauth-required"
        }
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

async function replayAfterUnauthorized<T>(
  attemptedToken: string | null,
  retry: () => Promise<T>,
  fallback: () => T,
): Promise<T> {
  const currentToken = getAccessToken()
  if (currentToken && attemptedToken && currentToken !== attemptedToken) {
    return retry()
  }
  const refreshed = await refreshSession()
  if (refreshed === "refreshed") return retry()
  if (refreshed === "unavailable") {
    throw new ApiRequestError(503, "AUTH_SESSION_UNAVAILABLE", "会话权威状态暂不可用，请稍后重试")
  }
  if (refreshed === "reauth-required") {
    throw new ApiRequestError(401, "AUTH_REAUTH_REQUIRED", "AD 会话已到期，请重新登录")
  }
  return fallback()
}

async function jsonAttempt<T>(url: string, init: RequestInit, timeoutMs: number): Promise<AuthorizedJsonResult<T>> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal)
    const body = await readJsonBody<T | ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
    return { status: response.status, ok: response.ok, headers: response.headers, body }
  } catch (error) {
    mapHttpBodyError(error)
  } finally {
    attempt.cleanup()
  }
}

export async function authorizedJsonResult<T>(
  url: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<AuthorizedJsonResult<T>> {
  const attemptedToken = getAccessToken()
  const first = await jsonAttempt<T>(url, init, timeoutMs)
  const decision = classifyAuthDecision(first.status, first.body)
  applyAuthDecision(decision)
  if (decision !== "unauthorized") return first
  try {
    return await replayAfterUnauthorized(
      attemptedToken,
      () => jsonAttempt<T>(url, init, timeoutMs),
      () => first,
    )
  } catch (error) {
    if (error instanceof ApiRequestError && error.code === "AUTH_SESSION_UNAVAILABLE") {
      return {
        status: 503,
        ok: false,
        headers: new Headers({ "Content-Type": "application/json" }),
        body: {
          code: "AUTH_SESSION_UNAVAILABLE",
          message: "会话权威状态暂不可用，请稍后重试",
          detail: null,
        },
      }
    }
    if (error instanceof ApiRequestError && error.code === "AUTH_REAUTH_REQUIRED") {
      return {
        status: 401,
        ok: false,
        headers: new Headers({ "Content-Type": "application/json" }),
        body: {
          code: "AUTH_REAUTH_REQUIRED",
          message: "AD 会话已到期，请重新登录",
          detail: null,
        },
      }
    }
    throw error
  }
}

async function rawAttempt(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<{ response: Response; body: ApiErrorBody | null }> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal)
    if (response.status === 401 || response.status === 409 || response.status === 423) {
      const body = await readJsonBody<ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
      return { response: rebuildJsonResponse(response, body), body }
    }
    return { response, body: null }
  } catch (error) {
    mapHttpBodyError(error)
  } finally {
    attempt.cleanup()
  }
}

export async function authorizedFetch(
  url: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const attemptedToken = getAccessToken()
  const first = await rawAttempt(url, init, timeoutMs)
  const decision = classifyAuthDecision(first.response.status, first.body)
  applyAuthDecision(decision)
  if (decision !== "unauthorized") return first.response
  try {
    const replayed = await replayAfterUnauthorized(
      attemptedToken,
      () => rawAttempt(url, init, timeoutMs),
      () => first,
    )
    return replayed.response
  } catch (error) {
    if (error instanceof ApiRequestError && error.code === "AUTH_SESSION_UNAVAILABLE") {
      return new Response(
        JSON.stringify({
          code: "AUTH_SESSION_UNAVAILABLE",
          message: "会话权威状态暂不可用，请稍后重试",
          detail: null,
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      )
    }
    if (error instanceof ApiRequestError && error.code === "AUTH_REAUTH_REQUIRED") {
      return new Response(
        JSON.stringify({
          code: "AUTH_REAUTH_REQUIRED",
          message: "AD 会话已到期，请重新登录",
          detail: null,
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      )
    }
    throw error
  }
}

type BlobAttempt = { kind: "blob"; blob: Blob } | { kind: "error"; status: number; body: ApiErrorBody | null }

async function blobAttempt(url: string, init: RequestInit, timeoutMs: number): Promise<BlobAttempt> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal)
    if (!response.ok) {
      let body: ApiErrorBody | null = null
      try {
        body = await readJsonBody<ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
      } catch (error) {
        if (!(error instanceof HttpBodyError)) throw error
      }
      return { kind: "error", status: response.status, body }
    }
    return {
      kind: "blob",
      blob: await readLimitedBlob(response, attempt.signal, DOWNLOAD_MAX_BYTES),
    }
  } catch (error) {
    mapHttpBodyError(error)
  } finally {
    attempt.cleanup()
  }
}

function throwDownloadError(status: number, body: ApiErrorBody | null): never {
  throw new ApiRequestError(
    status,
    body?.code || `HTTP_${status}`,
    body?.message || body?.code || `下载失败（${status}）`,
    body?.detail,
  )
}

export async function authorizedBlob(
  url: string,
  init: RequestInit,
  timeoutMs: number = DOWNLOAD_TIMEOUT_MS,
): Promise<Blob> {
  const attemptedToken = getAccessToken()
  const first = await blobAttempt(url, init, timeoutMs)
  if (first.kind === "blob") return first.blob
  const decision = classifyAuthDecision(first.status, first.body)
  applyAuthDecision(decision)
  if (decision !== "unauthorized") throwDownloadError(first.status, first.body)
  try {
    const replayed = await replayAfterUnauthorized(
      attemptedToken,
      () => blobAttempt(url, init, timeoutMs),
      () => first,
    )
    if (replayed.kind === "blob") return replayed.blob
    throwDownloadError(replayed.status, replayed.body)
  } catch (error) {
    if (error instanceof ApiRequestError && error.code === "AUTH_SESSION_UNAVAILABLE") {
      throwDownloadError(503, {
        code: "AUTH_SESSION_UNAVAILABLE",
        message: "会话权威状态暂不可用，请稍后重试",
        detail: null,
      })
    }
    if (error instanceof ApiRequestError && error.code === "AUTH_REAUTH_REQUIRED") {
      throwDownloadError(401, {
        code: "AUTH_REAUTH_REQUIRED",
        message: "AD 会话已到期，请重新登录",
        detail: null,
      })
    }
    throw error
  }
}

function unwrapAuthorizedJson<T>(result: AuthorizedJsonResult<T>): T {
  if (!result.ok) {
    const body = (result.body ?? {}) as ApiErrorBody
    throw new ApiRequestError(
      result.status,
      body.code || `HTTP_${result.status}`,
      body.message || body.code || `请求失败（${result.status}）`,
      body.detail,
    )
  }
  if (result.status === 204 || result.body == null) {
    return undefined as T
  }
  return result.body as T
}

/** Web 业务端点：path 自动加 `/api/v1/web` 前缀。 */
export async function apiRequest<T>(
  path: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<T> {
  return unwrapAuthorizedJson<T>(await authorizedJsonResult<T>(`/api/v1/web${path}`, init, timeoutMs))
}

/** 绝对路径端点（如 `/api/v1/messages/...`）：与 apiRequest 同错误类型，不加前缀。 */
export async function apiRequestAbs<T>(
  path: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_REQUEST_TIMEOUT_MS,
): Promise<T> {
  return unwrapAuthorizedJson<T>(await authorizedJsonResult<T>(path, init, timeoutMs))
}
