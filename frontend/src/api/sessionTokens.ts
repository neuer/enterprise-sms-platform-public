// Access Token 与用户快照的仅内存会话；不再写入 Web Storage。

import type { PlatformUser } from "./auth"

const TOKEN_KEY = "sms_token"
const USER_KEY = "sms_user"

let accessToken: string | null = null
let sessionUser: PlatformUser | null = null


export function getAccessToken(): string | null {
  return accessToken ?? sessionStorage.getItem(TOKEN_KEY)
}

export function getSessionUser(): PlatformUser | null {
  if (sessionUser) return sessionUser
  const raw = sessionStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as PlatformUser
  } catch {
    return null
  }
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
