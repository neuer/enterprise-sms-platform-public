/**
 * 颜色模式单点（深色/明亮一键切换）。
 * 主题只是视觉偏好：写入 html[data-theme] 供 CSS 令牌生效，
 * localStorage 仅存 "dark"|"light" 字符串，不涉及任何凭据（硬性规则 26 不适用）。
 * 切换时派发 `sms:theme-change` 事件，供 ECharts 等 canvas 组件重建配色。
 */

export type ThemeMode = "dark" | "light"

const STORAGE_KEY = "sms-theme"
export const THEME_CHANGE_EVENT = "sms:theme-change"

/** 读取持久化偏好；无记录或非法值一律回退深色（平台默认主题）。 */
export function getTheme(): ThemeMode {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark"
  } catch {
    return "dark"
  }
}

/** 应用主题到根元素并持久化；每次调用都广播，图表据此重建配色。 */
export function setTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode
  try {
    window.localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    // 隐私模式等写入失败时仅本次会话生效，不影响功能
  }
  window.dispatchEvent(new CustomEvent<ThemeMode>(THEME_CHANGE_EVENT, { detail: mode }))
}

export function toggleTheme(): ThemeMode {
  const next: ThemeMode = getTheme() === "light" ? "dark" : "light"
  setTheme(next)
  return next
}

/** 应用启动时恢复偏好（index.html 内联脚本已先行写入，此处幂等兜底）。 */
export function initTheme(): void {
  const mode = getTheme()
  if (document.documentElement.dataset.theme !== mode) {
    document.documentElement.dataset.theme = mode
  }
}
