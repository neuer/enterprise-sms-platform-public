import { AuthApiError, refreshRequest } from "./auth"

export type Category = "notice" | "market"

export interface SegmentPart {
  used: number
  capacity: number
  partial: boolean
}

export interface BillingPreview {
  final_length: number
  est_segments: number
  quota_cost: number
  segment_parts: SegmentPart[]
  next_segment_at: number
  approval_required: boolean
  unsubscribe_appended: boolean
  deferred_reason: string | null
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
}

interface ApiErrorBody {
  code?: string
  message?: string
}

const TOKEN_KEY = "sms_token"
const REFRESH_TOKEN_KEY = "sms_refresh_token"
const USER_KEY = "sms_user"
type RefreshResult = "refreshed" | "unauthorized" | "unavailable"
let refreshInFlight: Promise<RefreshResult> | null = null

export function authorization(): Record<string, string> {
  const token = sessionStorage.getItem(TOKEN_KEY)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function requestWithCurrentAuthorization(url: string, init: RequestInit): Promise<Response> {
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
  const token = sessionStorage.getItem(TOKEN_KEY)
  if (token) headers.Authorization = `Bearer ${token}`
  return fetch(url, { ...init, headers })
}

function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(REFRESH_TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
  window.dispatchEvent(new Event("sms:unauthorized"))
}

async function refreshSession(): Promise<RefreshResult> {
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = (async () => {
    const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_KEY)
    if (!refreshToken) {
      clearSession()
      return "unauthorized"
    }
    try {
      const result = await refreshRequest(refreshToken)
      sessionStorage.setItem(TOKEN_KEY, result.token)
      sessionStorage.setItem(REFRESH_TOKEN_KEY, result.refresh_token)
      sessionStorage.setItem(USER_KEY, JSON.stringify(result.user))
      window.dispatchEvent(new Event("sms:session-refreshed"))
      return "refreshed"
    } catch (error) {
      if (error instanceof AuthApiError && error.status === 401) {
        clearSession()
        return "unauthorized"
      }
      return "unavailable"
    }
  })()
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

export async function authorizedFetch(url: string, init: RequestInit): Promise<Response> {
  const attemptedToken = sessionStorage.getItem(TOKEN_KEY)
  const response = await requestWithCurrentAuthorization(url, init)
  const code = await errorCode(response)
  if (response.status === 423 && code === "ACCOUNT_LOCKED") {
    clearSession()
    return response
  }
  if (response.status !== 401 || code !== "UNAUTHORIZED") return response

  const currentToken = sessionStorage.getItem(TOKEN_KEY)
  if (currentToken && attemptedToken && currentToken !== attemptedToken) {
    return requestWithCurrentAuthorization(url, init)
  }
  const refreshed = await refreshSession()
  if (refreshed === "refreshed") {
    return requestWithCurrentAuthorization(url, init)
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
    throw new Error(body.message || body.code || `请求失败（${response.status}）`)
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
  if (result.status === "failed") throw new Error("号码文件解析失败，请检查格式后重试")
  return result
}

export async function downloadImportInvalidFile(url: string): Promise<Blob> {
  const response = await authorizedFetch(url, { method: "GET" })
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody
    throw new Error(body.message || body.code || `下载失败（${response.status}）`)
  }
  return response.blob()
}

export async function previewBilling(
  payload: Omit<WebMessagePayload, "mobiles" | "import_id" | "is_test" | "scheduled_at" | "remark"> & {
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
