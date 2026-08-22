// Access Token 与用户快照的仅内存会话；不再写入 Web Storage。

import type { PlatformUser } from "./auth"

const TOKEN_KEY = "sms_token"
const USER_KEY = "sms_user"
export const REFRESH_TAB_ID_KEY = "sms_refresh_tab_id"
const REFRESH_TAB_ID_PATTERN = /^[0-9a-f]{32}$/

let accessToken: string | null = null
let sessionUser: PlatformUser | null = null
let refreshTabId: string | null = null
let legacyMigrationAttempted = false
let legacyMigrationClosed = false

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

function readSessionStorage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function storageGet(key: string): string | null {
  const storage = readSessionStorage()
  if (!storage) return null
  try {
    return storage.getItem(key)
  } catch {
    return null
  }
}

function storageRemove(key: string): void {
  const storage = readSessionStorage()
  if (!storage) return
  try {
    storage.removeItem(key)
  } catch {
    // 内存会话才是当前权威；Storage 失败不得阻断销毁。
  }
}

function closeLegacyAccessMigration(): void {
  legacyMigrationClosed = true
  legacyMigrationAttempted = true
}

function migrateLegacyStorageOnce(): void {
  if (legacyMigrationClosed || legacyMigrationAttempted) return
  // 先关闭本 Document 扫描闸门，再碰 Storage，避免删除失败后被再次导入。
  legacyMigrationAttempted = true
  const legacyToken = storageGet(TOKEN_KEY)
  const rawUser = storageGet(USER_KEY)
  storageRemove(TOKEN_KEY)
  storageRemove(USER_KEY)
  if (legacyMigrationClosed) return
  if (!accessToken && legacyToken) accessToken = legacyToken
  if (!sessionUser && rawUser) {
    try {
      sessionUser = JSON.parse(rawUser) as PlatformUser
    } catch {
      sessionUser = null
    }
  }
}

/** 当前 Document 只扫描一次历史 sms_token/sms_user；clear/logout 后永久关闭。 */
export function bootstrapLegacyAccessSession(): void {
  migrateLegacyStorageOnce()
}

export function getAccessToken(): string | null {
  // 至多一次 Document 级迁移；之后只读模块内存，clear 后不得再导入残留 Bearer。
  bootstrapLegacyAccessSession()
  return accessToken
}

export function getSessionUser(): PlatformUser | null {
  bootstrapLegacyAccessSession()
  return sessionUser
}

export function setAccessSession(token: string, user: PlatformUser): void {
  // 新主体只写入模块内存；剩余 Storage 不得再覆盖当前会话。
  legacyMigrationAttempted = true
  accessToken = token
  sessionUser = user
}

export function clearAccessSession(): void {
  accessToken = null
  sessionUser = null
  closeLegacyAccessMigration()
  storageRemove(TOKEN_KEY)
  storageRemove(USER_KEY)
}

/** 测试隔离：模拟新 Document，允许再次一次性迁移。生产路径不得调用。 */
export function resetAccessSessionModule(): void {
  accessToken = null
  sessionUser = null
  refreshTabId = null
  legacyMigrationAttempted = false
  legacyMigrationClosed = false
}
