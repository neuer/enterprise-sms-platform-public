// Access Token 与用户快照的仅内存会话；不再写入 Web Storage。

import type { PlatformUser } from "./auth"

const TOKEN_KEY = "sms_token"
const USER_KEY = "sms_user"

let accessToken: string | null = null
let sessionUser: PlatformUser | null = null

function migrateLegacyStorage(): void {
  const legacyToken = sessionStorage.getItem(TOKEN_KEY)
  const rawUser = sessionStorage.getItem(USER_KEY)
  // 先销毁持久化凭据，再解析；任何异常都不能延长 Bearer 暴露窗口。
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
  if (!accessToken && legacyToken) accessToken = legacyToken
  if (!sessionUser && rawUser) {
    try {
      sessionUser = JSON.parse(rawUser) as PlatformUser
    } catch {
      sessionUser = null
    }
  }
}


export function getAccessToken(): string | null {
  migrateLegacyStorage()
  return accessToken
}

export function getSessionUser(): PlatformUser | null {
  migrateLegacyStorage()
  return sessionUser
}

export function setAccessSession(token: string, user: PlatformUser): void {
  accessToken = token
  sessionUser = user
}

export function clearAccessSession(): void {
  accessToken = null
  sessionUser = null
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
}
