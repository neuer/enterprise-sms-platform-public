import { SESSION_CLEAR_SIGNAL_KEY, parseSessionRetiredMessage } from "./sessionSignals"

export interface SessionLogoutResult {
  cleared: boolean
}

interface LoginRedirectRouter {
  currentRoute: { value: { path: string } }
  replace: (path: string) => Promise<unknown>
}

interface SessionSignalReceiver {
  applyRemoteSessionRetired: (raw: unknown) => boolean
}

/** 仅在本操作确实清掉原实例时跳转登录页；新会话保持当前路由。 */
export async function redirectToLoginIfCleared(cleared: boolean, router: LoginRedirectRouter): Promise<void> {
  if (!cleared) return
  if (router.currentRoute.value.path === "/login") return
  await router.replace("/login")
}

/**
 * App 退出按钮的收尾：网络成败都只按原实例是否被清来决定跳转与提示。
 */
export async function runAppLogout(options: {
  logout: () => Promise<SessionLogoutResult>
  isAuthenticated: () => boolean
  redirectToLogin: () => Promise<void>
  onUnconfirmedRevoke?: () => void
}): Promise<void> {
  try {
    const { cleared } = await options.logout()
    if (cleared) await options.redirectToLogin()
  } catch {
    if (options.isAuthenticated()) return
    options.onUnconfirmedRevoke?.()
    await options.redirectToLogin()
  }
}

/** StorageEvent 入口：removeItem 的 newValue=null 不是清理命令。 */
export function applyIncomingSessionSignal(
  session: SessionSignalReceiver,
  event: { key?: string | null; newValue?: string | null },
): boolean {
  if (event.key !== SESSION_CLEAR_SIGNAL_KEY) return false
  if (event.newValue == null) return false
  if (!parseSessionRetiredMessage(event.newValue)) return false
  return session.applyRemoteSessionRetired(event.newValue)
}
