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

export async function withRefreshLock<T>(run: () => Promise<T>, options: { signal?: AbortSignal } = {}): Promise<T> {
  return defaultSessionDocument.withSessionLock(run, options)
}

export const withSessionLock = withRefreshLock
