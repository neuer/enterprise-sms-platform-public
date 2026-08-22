const REFRESH_LOCK_NAME = "sms-refresh-rotation"

export async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  const locks = globalThis.navigator?.locks
  if (!locks || typeof locks.request !== "function") {
    return run()
  }
  return locks.request(REFRESH_LOCK_NAME, run)
}
