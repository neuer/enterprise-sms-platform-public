export type SessionMode = "refresh" | "access_only"

export const ACCESS_ONLY_SESSION_MESSAGE = "当前浏览器为短会话模式，页面刷新或会话到期需重新登录。"
/** @deprecated 使用 ACCESS_ONLY_SESSION_MESSAGE；保留别名以免旧测试断裂。 */
export const SAFE_SINGLE_TAB_MESSAGE = ACCESS_ONLY_SESSION_MESSAGE

/** login/refresh/logout/restore/BFCache 与 Refresh 共用同一把跨标签页锁。 */
export function hasWebLocks(): boolean {
  return typeof globalThis.navigator?.locks?.request === "function"
}

/** 官方能力检测：有 `navigator.locks.request` 才允许 refresh，否则 access_only。不用 UA。 */
export function detectSessionMode(): SessionMode {
  return hasWebLocks() ? "refresh" : "access_only"
}

export function isAccessOnlySessionMode(): boolean {
  return detectSessionMode() === "access_only"
}

export function isSafeSingleTabMode(): boolean {
  return isAccessOnlySessionMode()
}
