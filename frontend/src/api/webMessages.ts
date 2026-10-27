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

/**
 * 对应 POST /api/v1/web/billing/preview 200 的内联响应（无命名 schema）。
 * 契约把 deferred_reason/quota 标为可选（服务端默认值所致），本类型按运行时
 * 恒返回收紧为必选，故保留手写；SegmentPart/QuotaSummary 同为内联匿名对象，无生成对应物。
 */
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

/**
 * 导入任务状态：逐字段对应 GET /api/v1/web/messages/import/{import_id} 200
 * 内联响应（POST /messages/import 的 202 响应形状相同）。
 */
export type ImportResult =
  paths["/api/v1/web/messages/import/{import_id}"]["get"]["responses"]["200"]["content"]["application/json"]

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

/**
 * 把异步导入的机器码换成可操作的中文。IMPORT_TOO_LARGE 在库里同时覆盖字节与行数，
 * 状态接口不再区分具体原因，因此不能写成单一的「仅字节」或「仅行数」。
 * 未知码不回显原始载荷。
 */
export function importFailureMessage(code: string | null | undefined): string {
  switch (code) {
    case "IMPORT_TOO_LARGE":
      return "文件大小或号码行数超过上限，请拆分后重新上传。"
    case "IMPORT_FORMAT_INVALID":
      return "文件格式无法解析，请改用 CSV 或 XLSX 后重新上传。"
    case "IMPORT_PARSE_FAILED":
      return "号码文件解析失败，请检查内容后重新上传。"
    case "IMPORT_PENDING":
    case "IMPORT_RETRY_PENDING":
      return "号码文件仍在后台处理，请稍后重试。"
    case "IMPORT_STAGE_FAILED":
    case "IMPORT_QUEUE_UNAVAILABLE":
      return "号码文件暂未受理，请稍后重新上传。"
    default:
      return "号码文件处理失败，请重新上传或稍后再试。"
  }
}

export async function uploadPhones(file: File, signal?: AbortSignal): Promise<ImportResult> {
  const form = new FormData()
  form.append("file", file)
  let result = await apiRequest<ImportResult>("/messages/import", { method: "POST", body: form })
  const deadline = Date.now() + 120_000
  let delay = 250
  while (result.status === "pending" || result.status === "processing") {
    if (Date.now() >= deadline) {
      throw new ApiRequestError(0, "IMPORT_PENDING", importFailureMessage("IMPORT_PENDING"))
    }
    signal?.throwIfAborted()
    await new Promise((resolve) => window.setTimeout(resolve, delay))
    result = await apiRequest<ImportResult>(`/messages/import/${result.import_id}`, {
      method: "GET",
      signal,
    })
    delay = Math.min(1_000, delay * 2)
  }
  if (result.status === "failed") {
    const code = result.error || "IMPORT_FAILED"
    throw new ApiRequestError(0, code, importFailureMessage(result.error))
  }
  return result
}

export async function downloadImportInvalidFile(url: string, signal?: AbortSignal): Promise<Blob> {
  return authorizedBlob(url, { method: "GET", signal }, DOWNLOAD_TIMEOUT_MS)
}

export async function previewBilling(
  payload: Omit<WebMessagePayload, "mobiles" | "import_id" | "scheduled_at" | "remark" | "biz_id"> & {
    accepted_count: number
  },
  signal?: AbortSignal,
): Promise<BillingPreview> {
  return apiRequest<BillingPreview>("/billing/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  })
}

export async function sendWebMessage(payload: WebMessagePayload): Promise<SendResult> {
  return apiRequest<SendResult>("/messages/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}
