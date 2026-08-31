import { apiRequest } from "./client"

export type AppCategory = "verify" | "notice" | "market"

export interface FrequencyOverride {
  verify_per_minute?: number
  verify_per_day?: number
  market_per_day?: number
}

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

export interface ManagedApp {
  id: number
  name: string
  dept: string
  allowed_categories: AppCategory[]
  default_sign: string | null
  daily_quota: number
  rate_limit_per_min: number
  blacklist_check: boolean
  freq_override: FrequencyOverride | null
  allowed_ips: string[]
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
  allowed_categories: AppCategory[]
  default_sign: string | null
  daily_quota: number
  rate_limit_per_min: number
  blacklist_check: boolean
  freq_override: FrequencyOverride | null
  allowed_ips: string[]
  callback_url: string | null
  callback_report_enabled: boolean
  status?: 0 | 1
}

export const listApps = () => apiRequest<ManagedApp[]>("/admin/apps", { method: "GET" })

export const getApp = (id: number) =>
  apiRequest<ManagedApp>(`/admin/apps/${id}`, { method: "GET" })

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

export const disableApp = (id: number) =>
  apiRequest<void>(`/admin/apps/${id}`, { method: "DELETE" })

export const rotateAppKey = (id: number) =>
  apiRequest<{ api_key: string; old_key_expires_at: string }>(`/admin/apps/${id}/rotate-key`, { method: "POST" })

export const revokeOldAppKey = (id: number) =>
  apiRequest<void>(`/admin/apps/${id}/revoke-old-key`, { method: "POST" })

export const rotateCallbackSecret = (id: number) =>
  apiRequest<{ callback_secret: string }>(`/admin/apps/${id}/rotate-callback-secret`, { method: "POST" })
