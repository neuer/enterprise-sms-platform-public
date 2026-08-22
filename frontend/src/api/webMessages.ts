import { AuthApiError, refreshRequest } from "./auth"
import { withRefreshLock } from "./refreshLock"
import {
  clearAccessSession,
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  setAccessSession,
} from "./sessionTokens"

export type Category = "notice" | "market"

export interface SegmentPart {
  used: number
  capacity: number
  partial: boolean
}

export interface QuotaSummary {
  used: number
  limit: number
  remaining: number | null
}

export interface BillingPreview {
  final_length: number
  est_segments: number
  quota_cost: number
  segment_parts: SegmentPart[]
  next_segment_at: number
  approval_required: boolean
  unsubscribe_appended: boolean
  final_content: string
  deferred_reason: string | null
  quota: QuotaSummary | null
}

export interface ImportResult {
  import_id: string
  valid: number
  invalid: number
  duplicate: number
  blacklisted: number
  invalid_download_url: string | null
  expires_at: string
  status: "pending" | "processing" | "ready" | "failed"
  error: string | null
}

export interface WebMessagePayload {
  category: Category
  biz_id: string
  mobiles?: string[]
  import_id?: string
  content?: string
  template_id?: number
  template_params?: string[]
  sign_name?: string
  scheduled_at?: string
  is_test: boolean
  consent_confirmed: boolean
  remark?: string
}

export interface SendResult {
  batch_no: string
  status: "queued" | "scheduled" | "pending_approval"
  accepted: number
  quota_cost: number
  idempotent: boolean
  deferred_reason: string | null
  removed_duplicate?: number
  removed_blacklist?: number
  removed_freq_limit?: number
  est_segments?: number
  scheduled_at?: string | null
}

interface ApiErrorBody {
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
const DOWNLOAD_TIMEOUT_MS = 120_000
type RefreshResult = "refreshed" | "unauthorized" | "unavailable"
let refreshInFlight: Promise<RefreshResult> | null = null
let sessionEpoch = 0
const sessionControllers = new Set<AbortController>()
window.addEventListener("sms:session-clearing", () => {
  sessionEpoch += 1
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
  return fetchWithTimeout(url, { ...init, headers }, controller, timeoutMs).finally(() => {
    sessionControllers.delete(controller)
    if (externalSignal) externalSignal.removeEventListener("abort", abortFromExternal)
  })
}

function clearSession(): void {
  cancelSessionRequests()
  clearAccessSession()
  clearRefreshTabBinding()
  window.dispatchEvent(new Event("sms:unauthorized"))
}

async function refreshSession(): Promise<RefreshResult> {
  if (refreshInFlight) return refreshInFlight
  const epochAtRequest = sessionEpoch
  refreshInFlight = withRefreshLock(async () => {
    if (sessionEpoch !== epochAtRequest) {
      return "unauthorized"
    }
    const epoch = sessionEpoch
    try {
      const currentUser = getSessionUser()
      const controller = new AbortController()
      const timer = window.setTimeout(
        () => controller.abort(new DOMException("刷新超时", "TimeoutError")),
        REFRESH_TIMEOUT_MS,
      )
      let result: Awaited<ReturnType<typeof refreshRequest>>
      try {
        result = await refreshRequest(controller.signal)
      } finally {
        window.clearTimeout(timer)
      }
      if (sessionEpoch !== epoch) {
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
      if (sessionEpoch !== epoch) {
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

export async function apiRequest<T>(path: string, init: RequestInit): Promise<T> {
  const response = await authorizedFetch(`/api/v1/web${path}`, init)
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

export async function uploadPhones(file: File): Promise<ImportResult> {
  const form = new FormData()
  form.append("file", file)
  let result = await apiRequest<ImportResult>("/messages/import", { method: "POST", body: form })
  const deadline = Date.now() + 120_000
  let delay = 250
  while (result.status === "pending" || result.status === "processing") {
    if (Date.now() >= deadline) throw new Error("号码文件仍在后台解析，请稍后重试")
    await new Promise((resolve) => window.setTimeout(resolve, delay))
    result = await apiRequest<ImportResult>(`/messages/import/${result.import_id}`, {
      method: "GET",
    })
    delay = Math.min(1_000, delay * 2)
  }
  if (result.status === "failed") {
    throw new Error(result.error || "号码文件解析失败，请检查格式后重试")
  }
  return result
}

export async function downloadImportInvalidFile(url: string): Promise<Blob> {
  const response = await authorizedFetch(url, { method: "GET" }, DOWNLOAD_TIMEOUT_MS)
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody
    throw new Error(body.message || body.code || `下载失败（${response.status}）`)
  }
  return response.blob()
}

export async function previewBilling(
  payload: Omit<WebMessagePayload, "mobiles" | "import_id" | "is_test" | "scheduled_at" | "remark" | "biz_id"> & {
    accepted_count: number
  },
): Promise<BillingPreview> {
  return apiRequest<BillingPreview>("/billing/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}

export async function sendWebMessage(payload: WebMessagePayload): Promise<SendResult> {
  return apiRequest<SendResult>("/messages/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}
