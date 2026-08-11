// Access Token 与用户快照的仅内存会话；不再写入 Web Storage。

import type { PlatformUser } from "./auth"

const TOKEN_KEY = "sms_token"
const USER_KEY = "sms_user"
export const REFRESH_TAB_ID_KEY = "sms_refresh_tab_id"
const REFRESH_TAB_ID_PATTERN = /^[0-9a-f]{32}$/

let accessToken: string | null = null
let sessionUser: PlatformUser | null = null
let refreshTabId: string | null = null

function newRefreshTabId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")
}

export function beginRefreshTabBinding(): string {
  refreshTabId = newRefreshTabId()
  try {
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, refreshTabId)
  } catch {
    // 受限存储环境仅保留当前页面内存绑定；刷新页面后必须重新登录。
  }
  return refreshTabId
}

export function getRefreshTabBinding(): string | null {
  if (refreshTabId && REFRESH_TAB_ID_PATTERN.test(refreshTabId)) return refreshTabId
  try {
    const stored = sessionStorage.getItem(REFRESH_TAB_ID_KEY)
    if (stored && REFRESH_TAB_ID_PATTERN.test(stored)) {
      refreshTabId = stored
      return stored
    }
  } catch {
    return null
  }
  return null
}

export function clearRefreshTabBinding(): void {
  refreshTabId = null
  try {
    sessionStorage.removeItem(REFRESH_TAB_ID_KEY)
  } catch {
    // 内存状态已经清除。
  }
}

function migrateLegacyStorage(): void {
  const legacyToken = sessionStorage.getItem(TOKEN_KEY)
  const rawUser = sessionStorage.getItem(USER_KEY)
  // 先销毁持久化凭据，再解析；任何异常都不能延长 Bearer 暴露窗口。
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
  if (!accessToken && legacyToken) accessToken = legacyToken
  if (!sessionUser && rawUser) {
    try {
      sessionUser = JSON.parse(rawUser) as PlatformUser
    } catch {
      sessionUser = null
    }
  }
}


export function getAccessToken(): string | null {
  migrateLegacyStorage()
  return accessToken
}

export function getSessionUser(): PlatformUser | null {
  migrateLegacyStorage()
  return sessionUser
}

export function setAccessSession(token: string, user: PlatformUser): void {
  accessToken = token
  sessionUser = user
}

export function clearAccessSession(): void {
  accessToken = null
  sessionUser = null
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
}
