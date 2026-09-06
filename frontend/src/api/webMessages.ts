import { apiRequest, authorizedBlob, ApiRequestError, DOWNLOAD_TIMEOUT_MS } from "./client"
import type { paths } from "./types.gen"

/**
 * 契约类型逐步迁移说明：`types.gen.ts` 由 `npm run gen:api-types` 从根 openapi.yaml
 * 生成（CI 对生成产物做零漂移门禁，禁止手改）。src/api/ 下其余手写 interface 后续按
 * 模块逐个替换为 paths/operations 生成类型引用，迁移时以 SendResult 为范例。
 */

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

/**
 * `/api/v1/web/messages/send` 200 响应；字段与状态枚举（含 completed_unknown 等
 * 全部批次终态）以 openapi.yaml 为准，required 字段不得再标可选。
 */
export type SendResult = paths["/api/v1/web/messages/send"]["post"]["responses"]["200"]["content"]["application/json"]

export async function uploadPhones(file: File): Promise<ImportResult> {
  const form = new FormData()
  form.append("file", file)
  let result = await apiRequest<ImportResult>("/messages/import", { method: "POST", body: form })
  const deadline = Date.now() + 120_000
  let delay = 250
  while (result.status === "pending" || result.status === "processing") {
    if (Date.now() >= deadline) {
      throw new ApiRequestError(0, "IMPORT_PENDING", "号码文件仍在后台解析，请稍后重试")
    }
    await new Promise((resolve) => window.setTimeout(resolve, delay))
    result = await apiRequest<ImportResult>(`/messages/import/${result.import_id}`, {
      method: "GET",
    })
    delay = Math.min(1_000, delay * 2)
  }
  if (result.status === "failed") {
    throw new ApiRequestError(0, "IMPORT_FAILED", result.error || "号码文件解析失败，请检查格式后重试")
  }
  return result
}

export async function downloadImportInvalidFile(url: string): Promise<Blob> {
  return authorizedBlob(url, { method: "GET" }, DOWNLOAD_TIMEOUT_MS)
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
