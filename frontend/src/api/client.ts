import { SESSION_CLEARING_EVENT } from "./sessionEvents"
/**
 * 前端请求基建单点：同源断言、端到端 Deadline、Bearer 注入、
 * 401+UNAUTHORIZED 单飞刷新重放、会话代际联动取消。所有业务 api 模块统一使用
 * 本文件的 apiRequest/authorizedFetch/authorizedBlob，禁止再平行实现请求封装
 * （auth.ts 为 pre-auth 例外，见该文件注释）。
 */
import { AuthApiError, refreshRequest } from "./auth"
import { defaultSessionDocument } from "./sessionDocument"
import {
  API_JSON_MAX_BYTES,
  DOWNLOAD_MAX_BYTES,
  HttpBodyError,
  createDeadline,
  joinAbortSignals,
  readJsonBody,
  readLimitedBlob,
} from "./httpDeadline"
import { detectSessionMode, withRefreshLock } from "./refreshLock"
import {
  captureSessionOperationOrigin,
  getSessionGeneration,
  invalidateSessionGeneration,
  isCurrentSessionGeneration,
  isSessionOperationOriginCurrent,
  type SessionOperationOrigin,
  trackSessionController,
} from "./sessionGeneration"
import {
  clearAccessSession,
  clearRefreshTabBinding,
  getAccessToken,
  getSessionInstanceId,
  getSessionMode,
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
class RequestScope {
  readonly origin: SessionOperationOrigin
  retiredGeneration: number | null = null

  constructor() {
    // 一次性历史迁移必须先完成，随后固定本次逻辑请求的来源。
    getAccessToken()
    this.origin = captureSessionOperationOrigin()
  }

  assertCurrent(): void {
    if (isSessionOperationOriginCurrent(this.origin)) return
    if (this.retiredGeneration === getSessionGeneration() && getSessionInstanceId() === null) return
    throw new DOMException("会话已切换", "AbortError")
  }

  retire(reason: "unauthorized" | "reauth-required"): void {
    this.assertCurrent()
    if (clearSession(reason, this.origin)) this.retiredGeneration = getSessionGeneration()
  }
}

interface AttemptContext {
  scope: RequestScope
  token: string | null
}

const resultScopes = new WeakMap<object, RequestScope>()

function bindResult<T extends object>(result: T, scope: RequestScope): T {
  scope.assertCurrent()
  resultScopes.set(result, scope)
  return result
}

export function assertAuthorizedResultCurrent(result: object): void {
  resultScopes.get(result)?.assertCurrent()
}

interface RefreshFlight {
  scope: RequestScope
  promise: Promise<RefreshResult>
  queue: AbortController
  waiters: Set<symbol>
  started: () => boolean
}
let refreshInFlight: RefreshFlight | null = null

/** 仅最后一个等待者退出时撤销排队；已开始的 Cookie 轮换继续持锁完成。 */
function joinRefresh(flight: RefreshFlight, signal?: AbortSignal): Promise<RefreshResult> {
  const waiter = Symbol()
  flight.waiters.add(waiter)
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      flight.waiters.delete(waiter)
      signal?.removeEventListener("abort", cancel)
    }
    const cancel = () => {
      cleanup()
      if (!flight.waiters.size && !flight.started()) flight.queue.abort()
      reject(signal?.reason ?? new DOMException("请求已取消", "AbortError"))
    }
    signal?.addEventListener("abort", cancel, { once: true })
    if (signal?.aborted) cancel()
    flight.promise.then(
      (value) => {
        cleanup()
        resolve(value)
      },
      (error) => {
        cleanup()
        reject(error)
      },
    )
  })
}
const sessionControllers = new Set<AbortController>()
window.addEventListener(SESSION_CLEARING_EVENT, () => {
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

function authorizedHeaders(init: RequestInit, token: string | null): Record<string, string> {
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
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

async function fetchAuthorizedOnce(
  url: string,
  init: RequestInit,
  signal: AbortSignal,
  context: AttemptContext,
): Promise<Response> {
  assertSameOrigin(url)
  const headers = authorizedHeaders(init, context.token)
  context.scope.assertCurrent()
  if (signal.aborted) throw signal.reason
  return fetch(url, { ...init, headers, signal })
}

function attemptContext(scope: RequestScope): AttemptContext {
  scope.assertCurrent()
  const token = getAccessToken()
  scope.assertCurrent()
  return { scope, token }
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

function applyAuthDecision(decision: AuthDecision, scope: RequestScope): void {
  scope.assertCurrent()
  if (decision === "account-locked" || decision === "context-changed") {
    scope.retire("unauthorized")
  } else if (decision === "reauth-required") {
    scope.retire("reauth-required")
  }
}

function applyFinalAuthDecision(status: number, body: unknown, scope: RequestScope): void {
  const decision = classifyAuthDecision(status, body)
  applyAuthDecision(decision, scope)
  if (decision === "unauthorized") scope.retire("unauthorized")
}

function clearSession(
  broadcast: "unauthorized" | "reauth-required" | "none" = "unauthorized",
  origin?: SessionOperationOrigin,
): boolean {
  if (origin && !isSessionOperationOriginCurrent(origin)) return false
  const retiredInstance = getSessionInstanceId()
  invalidateSessionGeneration()
  cancelSessionRequests()
  clearAccessSession()
  clearRefreshTabBinding()
  window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
  if (retiredInstance) defaultSessionDocument.broadcastRetired(retiredInstance)
  if (broadcast === "unauthorized") {
    window.dispatchEvent(new Event("sms:unauthorized"))
  } else if (broadcast === "reauth-required") {
    window.dispatchEvent(new Event("sms:reauth-required"))
  }
  return true
}

async function refreshSession(scope: RequestScope, signal?: AbortSignal): Promise<RefreshResult> {
  scope.assertCurrent()
  signal?.throwIfAborted()
  const origin = scope.origin
  if (detectSessionMode() === "access_only" || getSessionMode() === "access_only") {
    scope.retire("unauthorized")
    return "unauthorized"
  }
  if (
    refreshInFlight &&
    !refreshInFlight.queue.signal.aborted &&
    isSessionOperationOriginCurrent(refreshInFlight.scope.origin)
  ) {
    const joined = refreshInFlight
    const result = await joinRefresh(joined, signal)
    scope.retiredGeneration = joined.scope.retiredGeneration
    scope.assertCurrent()
    return result
  }
  const epochAtRequest = getSessionGeneration()
  const queue = new AbortController()
  let started = false
  const flight: RefreshFlight = {
    scope,
    queue,
    waiters: new Set<symbol>(),
    started: () => started,
    promise: withRefreshLock(
      async () => {
        started = true
        scope.assertCurrent()
        if (!isCurrentSessionGeneration(epochAtRequest) || origin.sessionInstanceId !== getSessionInstanceId()) {
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
          if (!isCurrentSessionGeneration(epoch) || origin.sessionInstanceId !== getSessionInstanceId()) {
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
            scope.retire("unauthorized")
            return "unauthorized"
          }
          setAccessSession(result.token, result.user, result.session_mode, getSessionInstanceId() ?? undefined)
          window.dispatchEvent(new Event("sms:session-refreshed"))
          return "refreshed"
        } catch (error) {
          if (!isCurrentSessionGeneration(epoch) || origin.sessionInstanceId !== getSessionInstanceId()) {
            return "unauthorized"
          }
          if (error instanceof AuthApiError && error.status === 401) {
            if (error.code === "AUTH_REAUTH_REQUIRED") {
              scope.retire("reauth-required")
              return "reauth-required"
            }
            scope.retire("unauthorized")
            return "unauthorized"
          }
          return "unavailable"
        }
      },
      { signal: queue.signal },
    ),
  }
  refreshInFlight = flight
  const clear = () => {
    if (refreshInFlight === flight) refreshInFlight = null
  }
  void flight.promise.then(clear, clear)
  const result = await joinRefresh(flight, signal)
  scope.assertCurrent()
  return result
}

async function replayAfterUnauthorized<T>(
  scope: RequestScope,
  attemptedToken: string | null,
  retry: () => Promise<T>,
  fallback: () => T,
  signal?: AbortSignal,
): Promise<T> {
  scope.assertCurrent()
  if (getSessionMode() === "access_only" || detectSessionMode() === "access_only") {
    scope.retire("unauthorized")
    return fallback()
  }
  const currentToken = getAccessToken()
  if (currentToken && attemptedToken && currentToken !== attemptedToken) {
    return retry()
  }
  signal?.throwIfAborted()
  const refreshed = await refreshSession(scope, signal)
  scope.assertCurrent()
  signal?.throwIfAborted()
  if (refreshed === "refreshed") return retry()
  if (refreshed === "unavailable") {
    throw new ApiRequestError(503, "AUTH_SESSION_UNAVAILABLE", "会话权威状态暂不可用，请稍后重试")
  }
  if (refreshed === "reauth-required") {
    throw new ApiRequestError(401, "AUTH_REAUTH_REQUIRED", "AD 会话已到期，请重新登录")
  }
  return fallback()
}

async function jsonAttempt<T>(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  context: AttemptContext,
): Promise<AuthorizedJsonResult<T>> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal, context)
    const body = await readJsonBody<T | ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
    const result = { status: response.status, ok: response.ok, headers: response.headers, body }
    return bindResult(result, context.scope)
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    context.scope.assertCurrent()
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
  const scope = new RequestScope()
  const context = attemptContext(scope)
  const attemptedToken = context.token
  const first = await jsonAttempt<T>(url, init, timeoutMs, context)
  scope.assertCurrent()
  const decision = classifyAuthDecision(first.status, first.body)
  applyAuthDecision(decision, scope)
  if (decision !== "unauthorized") return first
  try {
    const replayed = await replayAfterUnauthorized(
      scope,
      attemptedToken,
      () => jsonAttempt<T>(url, init, timeoutMs, attemptContext(scope)),
      () => first,
      init.signal ?? undefined,
    )
    scope.assertCurrent()
    applyFinalAuthDecision(replayed.status, replayed.body, scope)
    return replayed
  } catch (error) {
    scope.assertCurrent()
    if (error instanceof ApiRequestError && error.code === "AUTH_SESSION_UNAVAILABLE") {
      return bindResult(
        {
          status: 503,
          ok: false,
          headers: new Headers({ "Content-Type": "application/json" }),
          body: {
            code: "AUTH_SESSION_UNAVAILABLE",
            message: "会话权威状态暂不可用，请稍后重试",
            detail: null,
          },
        },
        scope,
      )
    }
    if (error instanceof ApiRequestError && error.code === "AUTH_REAUTH_REQUIRED") {
      return bindResult(
        {
          status: 401,
          ok: false,
          headers: new Headers({ "Content-Type": "application/json" }),
          body: {
            code: "AUTH_REAUTH_REQUIRED",
            message: "AD 会话已到期，请重新登录",
            detail: null,
          },
        },
        scope,
      )
    }
    throw error
  }
}

async function rawAttempt(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  context: AttemptContext,
): Promise<{ response: Response; body: ApiErrorBody | null }> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal, context)
    if (response.status === 401 || response.status === 409 || response.status === 423) {
      const body = await readJsonBody<ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
      context.scope.assertCurrent()
      return { response: rebuildJsonResponse(response, body), body }
    }
    context.scope.assertCurrent()
    return { response, body: null }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    context.scope.assertCurrent()
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
  const scope = new RequestScope()
  const context = attemptContext(scope)
  const attemptedToken = context.token
  const first = await rawAttempt(url, init, timeoutMs, context)
  scope.assertCurrent()
  const decision = classifyAuthDecision(first.response.status, first.body)
  applyAuthDecision(decision, scope)
  if (decision !== "unauthorized") return first.response
  try {
    const replayed = await replayAfterUnauthorized(
      scope,
      attemptedToken,
      () => rawAttempt(url, init, timeoutMs, attemptContext(scope)),
      () => first,
      init.signal ?? undefined,
    )
    scope.assertCurrent()
    applyFinalAuthDecision(replayed.response.status, replayed.body, scope)
    return replayed.response
  } catch (error) {
    scope.assertCurrent()
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

async function blobAttempt(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  context: AttemptContext,
): Promise<BlobAttempt> {
  const attempt = startAuthorizedAttempt(timeoutMs, init.signal ?? undefined, "请求超时")
  try {
    const response = await fetchAuthorizedOnce(url, init, attempt.signal, context)
    if (!response.ok) {
      let body: ApiErrorBody | null = null
      try {
        body = await readJsonBody<ApiErrorBody>(response, attempt.signal, API_JSON_MAX_BYTES)
      } catch (error) {
        if (!(error instanceof HttpBodyError)) throw error
      }
      context.scope.assertCurrent()
      return { kind: "error", status: response.status, body }
    }
    const blob = await readLimitedBlob(response, attempt.signal, DOWNLOAD_MAX_BYTES)
    context.scope.assertCurrent()
    return { kind: "blob", blob }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    context.scope.assertCurrent()
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
  const scope = new RequestScope()
  const context = attemptContext(scope)
  const attemptedToken = context.token
  const first = await blobAttempt(url, init, timeoutMs, context)
  scope.assertCurrent()
  if (first.kind === "blob") return first.blob
  const decision = classifyAuthDecision(first.status, first.body)
  applyAuthDecision(decision, scope)
  if (decision !== "unauthorized") throwDownloadError(first.status, first.body)
  try {
    const replayed = await replayAfterUnauthorized(
      scope,
      attemptedToken,
      () => blobAttempt(url, init, timeoutMs, attemptContext(scope)),
      () => first,
      init.signal ?? undefined,
    )
    scope.assertCurrent()
    if (replayed.kind === "blob") return replayed.blob
    applyFinalAuthDecision(replayed.status, replayed.body, scope)
    throwDownloadError(replayed.status, replayed.body)
  } catch (error) {
    scope.assertCurrent()
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
