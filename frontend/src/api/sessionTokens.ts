// Access Token 与用户快照的仅内存会话；不再写入 Web Storage。

import type { PlatformUser } from "./auth"
import { isAccessOnlySessionMode, type SessionMode } from "./sessionMode"
import { defaultSessionDocument } from "./sessionDocument"
import { isSessionInstanceId } from "./sessionSignals"

/** 历史 Web Storage 凭据键（规则 26 一次性迁移 + 清除的唯一事实源）。 */
export const LEGACY_TOKEN_KEY = "sms_token"
export const LEGACY_USER_KEY = "sms_user"
export const REFRESH_TAB_ID_KEY = "sms_refresh_tab_id"
const REFRESH_TAB_ID_PATTERN = /^[0-9a-f]{32}$/

function newRefreshTabId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")
}

export function beginRefreshTabBinding(): string {
  if (isAccessOnlySessionMode()) {
    throw new Error("短会话模式不得建立 Refresh 标签页绑定")
  }
  defaultSessionDocument.refreshTabId = newRefreshTabId()
  try {
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, defaultSessionDocument.refreshTabId)
  } catch {
    // 受限存储环境仅保留当前页面内存绑定；刷新页面后必须重新登录。
  }
  return defaultSessionDocument.refreshTabId
}

export function getRefreshTabBinding(): string | null {
  if (isAccessOnlySessionMode() || defaultSessionDocument.sessionMode === "access_only") return null
  if (defaultSessionDocument.refreshTabId && REFRESH_TAB_ID_PATTERN.test(defaultSessionDocument.refreshTabId)) {
    return defaultSessionDocument.refreshTabId
  }
  try {
    const stored = sessionStorage.getItem(REFRESH_TAB_ID_KEY)
    if (stored && REFRESH_TAB_ID_PATTERN.test(stored)) {
      defaultSessionDocument.refreshTabId = stored
      return stored
    }
  } catch {
    return null
  }
  return null
}

export function clearRefreshTabBinding(): void {
  defaultSessionDocument.clearRefreshTabBinding()
}

/** 当前 Document 只扫描一次历史 sms_token/sms_user；clear/logout 后永久关闭。 */
export function bootstrapLegacyAccessSession(): void {
  defaultSessionDocument.bootstrapLegacyAccessSession()
}

export function getAccessToken(): string | null {
  return defaultSessionDocument.getAccessToken()
}

export function getSessionUser(): PlatformUser | null {
  return defaultSessionDocument.getSessionUser()
}

export function getSessionMode(): SessionMode | null {
  return defaultSessionDocument.sessionMode
}

export function getSessionInstanceId(): string | null {
  const instance = defaultSessionDocument.getSessionInstanceId()
  return isSessionInstanceId(instance) ? instance : null
}

export function setAccessSession(
  token: string,
  user: PlatformUser,
  mode: SessionMode = "refresh",
  sessionInstanceId?: string,
): void {
  defaultSessionDocument.setAccessSession(token, user, mode, sessionInstanceId)
}

export function clearAccessSession(): void {
  defaultSessionDocument.clearAccessSession()
}

/** 测试隔离：模拟新 Document，允许再次一次性迁移。生产路径不得调用。 */
export function resetAccessSessionModule(): void {
  defaultSessionDocument.reset()
}
