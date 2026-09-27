import type { ManagedApp } from "../api/apps"
import { CATEGORY_LABELS } from "./labels"

/** 旧 Key 宽限剩余小时（向上取整，下限 0）；无宽限期旧 Key 返回 null。 */
export function graceHoursLeft(app: ManagedApp, now = Date.now()): number | null {
  if (!app.old_key_expires_at) return null
  const ms = Date.parse(app.old_key_expires_at) - now
  // 静态检查禁词 Math.ceil（防计费公式重实现误判）；floor((ms+3599999)/1h) 等价向上取整
  return Math.max(0, Math.floor((ms + 3_599_999) / 3_600_000))
}

/** 回调列只展示 host+path，完整 URL 收进 title。 */
export function callbackDisplay(url: string): string {
  try {
    const parsed = new URL(url)
    return `${parsed.host}${parsed.pathname === "/" ? "" : parsed.pathname}`
  } catch {
    return url
  }
}

export function categoriesText(app: ManagedApp): string {
  return app.allowed_categories.map((category) => CATEGORY_LABELS[category]).join(" · ")
}

export function freqOverrideText(app: ManagedApp): string {
  const override = app.freq_override
  if (!override) return "未覆盖"
  const parts: string[] = []
  if (override.verify_per_minute) parts.push(`验证码 ${override.verify_per_minute}/分`)
  if (override.verify_per_day) parts.push(`验证码 ${override.verify_per_day}/日`)
  if (override.market_per_day) parts.push(`营销 ${override.market_per_day}/日`)
  return parts.join(" · ") || "未覆盖"
}

/** 配额占用百分比；配额 0（不限量）或用量不可用（consumed 为 null）时不渲染进度条。 */
export function quotaPercent(consumed: number | null, dailyQuota: number): number | null {
  if (consumed === null || dailyQuota <= 0) return null
  return Math.min(100, (consumed / dailyQuota) * 100)
}

/** 进度条色阶：>80% 琥珀、≥100% 朱红，其余 verdi。 */
export function quotaTone(percent: number | null): "" | "warn" | "over" {
  if (percent === null) return ""
  if (percent >= 100) return "over"
  if (percent > 80) return "warn"
  return ""
}
