import { defaultSessionDocument } from "./sessionDocument"
import {
  ACCESS_ONLY_SESSION_MESSAGE,
  detectSessionMode,
  hasWebLocks,
  isAccessOnlySessionMode,
  type SessionMode,
} from "./sessionMode"

export { ACCESS_ONLY_SESSION_MESSAGE, detectSessionMode, hasWebLocks, isAccessOnlySessionMode }
export type { SessionMode }

/** 与 SessionDocument.withSessionLock 同一把跨标签页锁；无 Web Locks 时退化为本页互斥。 */
export async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  return defaultSessionDocument.withSessionLock(run)
}

export const withSessionLock = withRefreshLock
