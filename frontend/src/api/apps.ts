import { apiRequest } from "./client"
import type { MessageCategory } from "../lib/labels"
import type { components } from "./types.gen"

export type FrequencyOverride = components["schemas"]["FrequencyOverride"]

const frequencyOverrideKeys = new Set<keyof FrequencyOverride>([
  "verify_per_minute",
  "verify_per_day",
  "market_per_day",
])

export function parseFrequencyOverride(input: string): FrequencyOverride | null {
  if (!input.trim()) return null
  const parsed = JSON.parse(input) as unknown
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("频控覆盖必须是 JSON 对象")
  }
  const entries = Object.entries(parsed as Record<string, unknown>)
  if (entries.some(([key]) => !frequencyOverrideKeys.has(key as keyof FrequencyOverride))) {
    throw new Error("频控覆盖包含未支持的配置键")
  }
  if (entries.some(([, value]) => !Number.isInteger(value) || Number(value) < 1)) {
    throw new Error("频控覆盖值必须是正整数")
  }
  return Object.fromEntries(entries) as FrequencyOverride
}

/**
 * 与生成契约 App 的实质差异：契约把 default_sign、freq_override、allowed_ips、
 * ip_allowlist_exempt_until、unlimited_quota_exempt_until、admission_exempt_note、
 * callback_url、old_key_prefix、old_key_expires_at 标为可选（服务端默认值所致）；
 * 运行时响应恒返回这些字段，本类型按实际响应收紧为必选，故保留手写。
 */
export interface ManagedApp {
  id: number
  name: string
  dept: string
  allowed_categories: MessageCategory[]
  default_sign: string | null
  daily_quota: number
  rate_limit_per_min: number
  recipient_limit_per_min: number
  segment_limit_per_min: number
  max_in_flight_chunks: number
  allow_market_api_bulk: boolean
  blacklist_check: boolean
  freq_override: FrequencyOverride | null
  allowed_ips: string[]
  ip_allowlist_exempt_until: string | null
  unlimited_quota_exempt_until: string | null
  admission_exempt_note: string | null
  callback_url: string | null
  callback_report_enabled: boolean
  status: 0 | 1
  /** 当前 API Key 的 8 位前缀（非密元数据；已停用应用为字面量 revoked0） */
  api_key_prefix: string
  /** 宽限期旧 Key 前缀；null 表示无旧 Key */
  old_key_prefix: string | null
  /** 宽限期旧 Key 到期时间（ISO）；null 表示无旧 Key */
  old_key_expires_at: string | null
  callback_secret_configured: boolean
  created_at: string
}

export interface AppPayload {
  name?: string
  dept: string
  allowed_categories: MessageCategory[]
  default_sign: string | null
  daily_quota: number
  rate_limit_per_min: number
  recipient_limit_per_min: number
  segment_limit_per_min: number
  max_in_flight_chunks: number
  allow_market_api_bulk: boolean
  blacklist_check: boolean
  freq_override: FrequencyOverride | null
  allowed_ips: string[]
  ip_allowlist_exempt_until: string | null
  unlimited_quota_exempt_until: string | null
  admission_exempt_note: string | null
  callback_url: string | null
  callback_report_enabled: boolean
  status?: 0 | 1
}

const MAX_RECIPIENTS_PER_REQUEST = 10_000

export function estimateWorstCaseCapacity(input: {
  rate_limit_per_min: number
  recipient_limit_per_min: number
  segment_limit_per_min: number
  daily_quota: number
}): { recipientsPerMin: number; segmentsPerMin: number; dailySegments: number | null } {
  return {
    recipientsPerMin: Math.min(input.recipient_limit_per_min, input.rate_limit_per_min * MAX_RECIPIENTS_PER_REQUEST),
    segmentsPerMin: input.segment_limit_per_min,
    dailySegments: input.daily_quota > 0 ? input.daily_quota : null,
  }
}

export const listApps = (signal?: AbortSignal) => apiRequest<ManagedApp[]>("/admin/apps", { method: "GET", signal })

export const getApp = (id: number) => apiRequest<ManagedApp>(`/admin/apps/${id}`, { method: "GET" })

export const createApp = (payload: AppPayload & { name: string }) =>
  apiRequest<{ id: number; api_key: string; callback_secret: string | null }>("/admin/apps", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })

export const updateApp = (id: number, payload: AppPayload) =>
  apiRequest<ManagedApp>(`/admin/apps/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })

export const disableApp = (id: number) => apiRequest<void>(`/admin/apps/${id}`, { method: "DELETE" })

export const rotateAppKey = (id: number) =>
  apiRequest<{ api_key: string; old_key_expires_at: string }>(`/admin/apps/${id}/rotate-key`, { method: "POST" })

export const revokeOldAppKey = (id: number) => apiRequest<void>(`/admin/apps/${id}/revoke-old-key`, { method: "POST" })

export const rotateCallbackSecret = (id: number) =>
  apiRequest<{ callback_secret: string }>(`/admin/apps/${id}/rotate-callback-secret`, { method: "POST" })
