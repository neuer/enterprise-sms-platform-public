const REFRESH_LOCK_NAME = "sms-refresh-rotation"

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

let inPageBusy = false
const inPageWaiters: Array<() => void> = []

async function withInPageMutex<T>(run: () => Promise<T>): Promise<T> {
  if (inPageBusy) {
    await new Promise<void>((resolve) => {
      inPageWaiters.push(resolve)
    })
  }
  inPageBusy = true
  try {
    return await run()
  } finally {
    const next = inPageWaiters.shift()
    if (next) next()
    else inPageBusy = false
  }
}

export async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  const locks = globalThis.navigator?.locks
  if (locks && typeof locks.request === "function") {
    return locks.request(REFRESH_LOCK_NAME, run)
  }
  // 无 Web Locks 时只做本页串行；跨标签页 Cookie Writer 由 Access-Only 协议消除。
  return withInPageMutex(run)
}

export const withSessionLock = withRefreshLock
