import { defineStore } from "pinia"

import {
  AuthApiError,
  loginRequest,
  logoutRequest,
  passwordChangeRequest,
  providerRequest,
  refreshRequest,
  type AuthProvider,
  type PlatformUser,
  type UserRole,
} from "../api/auth"
import {
  invalidateSessionGeneration,
  SessionGenerationStaleError,
  withSessionGeneration,
} from "../api/sessionGeneration"
import {
  bootstrapLegacyAccessSession,
  clearAccessSession,
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  LEGACY_TOKEN_KEY,
  LEGACY_USER_KEY,
  setAccessSession,
} from "../api/sessionTokens"
import { ROLE_LABELS } from "../lib/labels"

const CHANGE_TOKEN_KEY = "sms_change_token"
const CHANGE_TOKEN_EXPIRES_AT_KEY = "sms_change_token_expires_at"
export const SESSION_CLEAR_SIGNAL_KEY = "sms_session_clear"

function readStorage(name: "localStorage" | "sessionStorage"): Storage | null {
  try {
    return window[name]
  } catch {
    return null
  }
}

function storageRemove(name: "localStorage" | "sessionStorage", key: string): void {
  const storage = readStorage(name)
  if (!storage) return
  try {
    storage.removeItem(key)
  } catch {
    // 单个 Storage 失败不得跳过其余清理或内存凭据销毁。
  }
}

function broadcastSessionClear(): void {
  try {
    const storage = readStorage("localStorage")
    if (!storage) return
    storage.setItem(SESSION_CLEAR_SIGNAL_KEY, String(Date.now()))
    storage.removeItem(SESSION_CLEAR_SIGNAL_KEY)
  } catch {
    // 当前标签页仍由服务端权威会话保护；受限存储环境无法通知兄弟标签页。
  }
}

function clearLegacyPersistence(): void {
  for (const key of [
    LEGACY_TOKEN_KEY,
    "sms_refresh_token",
    LEGACY_USER_KEY,
    CHANGE_TOKEN_KEY,
    CHANGE_TOKEN_EXPIRES_AT_KEY,
  ]) {
    storageRemove("localStorage", key)
    storageRemove("sessionStorage", key)
  }
}

function isPlatformUser(value: unknown): value is PlatformUser {
  if (!value || typeof value !== "object") return false
  const user = value as Record<string, unknown>
  return (
    Number.isInteger(user.account_id) &&
    Number(user.account_id) > 0 &&
    Number.isInteger(user.identity_id) &&
    Number(user.identity_id) > 0 &&
    typeof user.provider_code === "string" &&
    typeof user.username === "string" &&
    typeof user.display_name === "string" &&
    typeof user.dept === "string" &&
    ["admin", "approver", "operator", "viewer"].includes(String(user.role))
  )
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    token: getAccessToken() ?? "",
    accountId: getSessionUser()?.account_id ?? 0,
    identityId: getSessionUser()?.identity_id ?? 0,
    providerCode: getSessionUser()?.provider_code ?? "",
    username: getSessionUser()?.username ?? "",
    displayName: getSessionUser()?.display_name ?? "",
    dept: getSessionUser()?.dept ?? "",
    role: (getSessionUser()?.role ?? null) as UserRole | null,
    providers: [] as AuthProvider[],
  }),
  getters: {
    isAuthenticated: (state) =>
      Boolean(state.token && state.accountId > 0 && state.identityId > 0 && state.role),
    roleLabel: (state) => (state.role ? ROLE_LABELS[state.role] : "未登录"),
  },
  actions: {
    resetIdentity() {
      this.token = ""
      this.accountId = 0
      this.identityId = 0
      this.providerCode = ""
      this.username = ""
      this.displayName = ""
      this.dept = ""
      this.role = null
      clearAccessSession()
    },
    apply(token: string, user: PlatformUser) {
      clearLegacyPersistence()
      this.token = token
      this.accountId = user.account_id
      this.identityId = user.identity_id
      this.providerCode = user.provider_code
      this.username = user.username
      this.displayName = user.display_name
      this.dept = user.dept
      this.role = user.role
      setAccessSession(token, user)
    },
    clear() {
      try {
        // 先推进本页代际并取消在途 Refresh，避免旧响应在清状态后写回。
        invalidateSessionGeneration()
      } finally {
        this.resetIdentity()
        clearRefreshTabBinding()
        try {
          window.dispatchEvent(new Event("sms:session-clearing"))
        } finally {
          clearLegacyPersistence()
        }
      }
    },
    clearAllTabs() {
      this.clear()
      broadcastSessionClear()
    },
    restore() {
      // 历史凭据只允许当前 Document 扫描一次；clear/logout 后不得再从 Storage 导入。
      bootstrapLegacyAccessSession()
      clearLegacyPersistence()
      const memoryToken = getAccessToken()
      const memoryUser = getSessionUser()
      if (memoryToken && memoryUser && isPlatformUser(memoryUser)) {
        this.apply(memoryToken, memoryUser)
        return
      }
      this.resetIdentity()
    },
    async restoreFromCookie(): Promise<boolean> {
      if (this.token) return true
      try {
        return await withSessionGeneration({}, async ({ isLive, signal }) => {
          if (this.token) return true
          const result = await refreshRequest(signal)
          if (!isLive()) return false
          this.apply(result.token, result.user)
          return true
        })
      } catch (error) {
        if (error instanceof AuthApiError && error.code === "AUTH_REAUTH_REQUIRED") {
          window.dispatchEvent(new Event("sms:reauth-required"))
        }
        this.clear()
        return false
      }
    },
    async revalidateOnResume(): Promise<boolean> {
      try {
        return await withSessionGeneration({ invalidateFirst: true }, async ({ isLive, signal }) => {
          const result = await refreshRequest(signal)
          if (!isLive()) return false
          this.apply(result.token, result.user)
          return true
        })
      } catch (error) {
        if (error instanceof AuthApiError && error.code === "AUTH_REAUTH_REQUIRED") {
          window.dispatchEvent(new Event("sms:reauth-required"))
        }
        this.clearAllTabs()
        return false
      }
    },
    async loadProviders() {
      this.providers = await providerRequest()
    },
    async login(
      providerCode: string,
      username: string,
      password: string,
    ): Promise<
      | { nextAction: "authenticated" }
      | { nextAction: "change_password"; changeToken: string; expiresAt: number }
    > {
      try {
        return await withSessionGeneration({ invalidateFirst: true }, async ({ isLive, signal }) => {
          const response = await loginRequest(providerCode, username, password, signal)
          if (!isLive()) throw new SessionGenerationStaleError()
          if ("next_action" in response) {
            this.clear()
            return {
              nextAction: "change_password",
              changeToken: response.change_token,
              expiresAt: Date.now() + response.expires_in * 1000,
            }
          }
          this.apply(response.token, response.user)
          // 登录会覆盖浏览器级 Refresh Cookie；兄弟标签页必须销毁旧主体。
          broadcastSessionClear()
          return { nextAction: "authenticated" }
        })
      } catch (error) {
        if (error instanceof SessionGenerationStaleError) {
          this.clear()
        }
        throw error
      }
    },
    async changePassword(currentPassword: string, newPassword: string) {
      if (!this.token || this.providerCode !== "local") {
        throw new Error("仅已登录的本地账号可修改密码")
      }
      try {
        await passwordChangeRequest(this.token, currentPassword, newPassword)
      } catch (error) {
        if (error instanceof AuthApiError && error.code === "AUTH_CONTEXT_CHANGED") {
          this.clearAllTabs()
          window.dispatchEvent(new Event("sms:unauthorized"))
        }
        throw error
      }
      this.clear()
    },
    async logout() {
      const token = this.token
      try {
        if (token) {
          await withSessionGeneration({ invalidateFirst: true }, async ({ signal }) => {
            await logoutRequest(token, signal)
          })
        }
      } finally {
        // 无论服务端是否确认撤销，本地凭据都必须先销毁。
        this.clearAllTabs()
      }
    },
  },
})
