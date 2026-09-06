import { defaultSessionDocument } from "./sessionDocument"
import {
  ACCESS_ONLY_SESSION_MESSAGE,
  detectSessionMode,
  hasWebLocks,
  isAccessOnlySessionMode,
  isSafeSingleTabMode,
  SAFE_SINGLE_TAB_MESSAGE,
  type SessionMode,
} from "./sessionMode"

export {
  ACCESS_ONLY_SESSION_MESSAGE,
  detectSessionMode,
  hasWebLocks,
  isAccessOnlySessionMode,
  isSafeSingleTabMode,
  SAFE_SINGLE_TAB_MESSAGE,
}
export type { SessionMode }

const REFRESH_LOCK_NAME = "sms-refresh-rotation"

export async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  const locks = globalThis.navigator?.locks
  if (locks && typeof locks.request === "function") {
    return locks.request(REFRESH_LOCK_NAME, run)
  }
  // 无 Web Locks 时只做本页串行；与 Store 共用同一 Document 互斥。
  return defaultSessionDocument.withLocalMutex(run)
}

export const withSessionLock = withRefreshLock
