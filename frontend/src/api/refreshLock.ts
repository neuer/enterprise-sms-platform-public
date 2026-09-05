const REFRESH_LOCK_NAME = "sms-refresh-rotation"

export const SAFE_SINGLE_TAB_MESSAGE = "当前浏览器不支持跨标签页会话互斥。仅允许单标签页登录，刷新页面后需要重新登录。"

/** login/refresh/logout/restore/BFCache 与 Refresh 共用同一把跨标签页锁。 */
export function hasWebLocks(): boolean {
  return typeof globalThis.navigator?.locks?.request === "function"
}

export function isSafeSingleTabMode(): boolean {
  return !hasWebLocks()
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
  // 无 Web Locks 时禁止静默并行；只做本页串行，并配合安全单标签页合同。
  return withInPageMutex(run)
}

export const withSessionLock = withRefreshLock
