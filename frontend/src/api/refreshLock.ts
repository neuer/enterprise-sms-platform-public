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

export async function withRefreshLock<T>(run: () => Promise<T>, options: { signal?: AbortSignal } = {}): Promise<T> {
  return defaultSessionDocument.withSessionLock(run, options)
}

export const withSessionLock = withRefreshLock
