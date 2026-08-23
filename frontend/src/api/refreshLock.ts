const REFRESH_LOCK_NAME = "sms-refresh-rotation"

/** login/refresh/logout/restore/BFCache 与 Refresh 共用同一把跨标签页锁。 */
export async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  const locks = globalThis.navigator?.locks
  if (!locks || typeof locks.request !== "function") {
    return run()
  }
  return locks.request(REFRESH_LOCK_NAME, run)
}

export const withSessionLock = withRefreshLock
