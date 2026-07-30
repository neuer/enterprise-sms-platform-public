import { defineStore } from "pinia"

import {
  loginRequest,
  logoutRequest,
  passwordChangeRequest,
  providerRequest,
  type AuthProvider,
  type PlatformUser,
  type UserRole,
} from "../api/auth"

const TOKEN_KEY = "sms_token"
const REFRESH_TOKEN_KEY = "sms_refresh_token"
const USER_KEY = "sms_user"
const CHANGE_TOKEN_KEY = "sms_change_token"
const CHANGE_TOKEN_EXPIRES_AT_KEY = "sms_change_token_expires_at"
export const SESSION_CLEAR_SIGNAL_KEY = "sms_session_clear"

function clearLegacyPersistence(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(CHANGE_TOKEN_KEY)
  localStorage.removeItem(CHANGE_TOKEN_EXPIRES_AT_KEY)
  sessionStorage.removeItem(CHANGE_TOKEN_KEY)
  sessionStorage.removeItem(CHANGE_TOKEN_EXPIRES_AT_KEY)
}

const roleLabels: Record<UserRole, string> = {
  admin: "管理员",
  approver: "审批员",
  operator: "操作员",
  viewer: "查看员",
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
    token: "",
    refreshToken: "",
    accountId: 0,
    identityId: 0,
    providerCode: "",
    username: "",
    displayName: "",
    dept: "",
    role: null as UserRole | null,
    providers: [] as AuthProvider[],
  }),
  getters: {
    isAuthenticated: (state) =>
      Boolean(state.token && state.accountId > 0 && state.identityId > 0 && state.role),
    roleLabel: (state) => (state.role ? roleLabels[state.role] : "未登录"),
  },
  actions: {
    resetIdentity() {
      this.token = ""
      this.refreshToken = ""
      this.accountId = 0
      this.identityId = 0
      this.providerCode = ""
      this.username = ""
      this.displayName = ""
      this.dept = ""
      this.role = null
      sessionStorage.removeItem(TOKEN_KEY)
      sessionStorage.removeItem(REFRESH_TOKEN_KEY)
      sessionStorage.removeItem(USER_KEY)
    },
    apply(token: string, refreshToken: string, user: PlatformUser) {
      clearLegacyPersistence()
      this.token = token
      this.refreshToken = refreshToken
      this.accountId = user.account_id
      this.identityId = user.identity_id
      this.providerCode = user.provider_code
      this.username = user.username
      this.displayName = user.display_name
      this.dept = user.dept
      this.role = user.role
      sessionStorage.setItem(TOKEN_KEY, token)
      sessionStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
      sessionStorage.setItem(USER_KEY, JSON.stringify(user))
    },
    clear() {
      clearLegacyPersistence()
      this.resetIdentity()
    },
    clearAllTabs() {
      this.clear()
      try {
        localStorage.setItem(SESSION_CLEAR_SIGNAL_KEY, String(Date.now()))
        localStorage.removeItem(SESSION_CLEAR_SIGNAL_KEY)
      } catch {
        // 当前标签页仍已清理；受限存储环境由服务端 JWT 撤销继续 fail closed。
      }
    },
    restore() {
      clearLegacyPersistence()
      const token = sessionStorage.getItem(TOKEN_KEY)
      const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_KEY)
      const rawUser = sessionStorage.getItem(USER_KEY)
      if (!token || !refreshToken || !rawUser) {
        this.clear()
        return
      }
      try {
        const user: unknown = JSON.parse(rawUser)
        if (!isPlatformUser(user)) {
          this.clear()
          return
        }
        this.apply(token, refreshToken, user)
      } catch {
        this.clear()
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
      const response = await loginRequest(providerCode, username, password)
      if ("next_action" in response) {
        clearLegacyPersistence()
        this.resetIdentity()
        return {
          nextAction: "change_password",
          changeToken: response.change_token,
          expiresAt: Date.now() + response.expires_in * 1000,
        }
      }
      this.apply(response.token, response.refresh_token, response.user)
      return { nextAction: "authenticated" }
    },
    async changePassword(currentPassword: string, newPassword: string) {
      if (!this.token || this.providerCode !== "local") {
        throw new Error("仅已登录的本地账号可修改密码")
      }
      await passwordChangeRequest(this.token, currentPassword, newPassword)
      this.clear()
    },
    async logout() {
      const token = this.token
      try {
        if (token) await logoutRequest(token)
      } catch {
        // 服务不可达或令牌已失效时仍必须清除浏览器会话。
      } finally {
        this.clearAllTabs()
      }
    },
  },
})
