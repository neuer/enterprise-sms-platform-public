import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"
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
  type SessionMode,
  type UserRole,
} from "../api/auth"
import { detectSessionMode, isAccessOnlySessionMode } from "../api/refreshLock"
import { defaultSessionDocument, type SessionDocument } from "../api/sessionDocument"
import { SessionGenerationStaleError } from "../api/sessionGeneration"
import { applyIncomingSessionSignal, type SessionLogoutResult } from "../api/sessionNavigation"
import {
  createSessionInstanceId,
  isSessionInstanceId,
  parseSessionRetiredMessage,
  SESSION_CLEAR_SIGNAL_KEY,
} from "../api/sessionSignals"
import { LEGACY_TOKEN_KEY, LEGACY_USER_KEY } from "../api/sessionTokens"
import { ROLE_LABELS } from "../lib/labels"

const CHANGE_TOKEN_KEY = "sms_change_token"
const CHANGE_TOKEN_EXPIRES_AT_KEY = "sms_change_token_expires_at"
export { SESSION_CLEAR_SIGNAL_KEY }

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

function isSessionAbort(error: unknown): boolean {
  return error instanceof SessionGenerationStaleError || (error instanceof DOMException && error.name === "AbortError")
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

export function createSessionStore(doc: SessionDocument = defaultSessionDocument, storeId = "session") {
  const useStore = defineStore(storeId, {
    state: () => ({
      token: doc.getAccessToken() ?? "",
      accountId: doc.getSessionUser()?.account_id ?? 0,
      identityId: doc.getSessionUser()?.identity_id ?? 0,
      providerCode: doc.getSessionUser()?.provider_code ?? "",
      username: doc.getSessionUser()?.username ?? "",
      displayName: doc.getSessionUser()?.display_name ?? "",
      dept: doc.getSessionUser()?.dept ?? "",
      role: (doc.getSessionUser()?.role ?? null) as UserRole | null,
      sessionMode: (doc.sessionMode ?? null) as SessionMode | null,
      sessionInstanceId: doc.getSessionInstanceId() ?? "",
      providers: [] as AuthProvider[],
    }),
    getters: {
      isAdmin: (state) => state.role === "admin",
      canWrite: (state) => state.role === "admin" || state.role === "operator",
      canDecrypt: (state) => state.role === "admin" || state.role === "approver",
      canApprove: (state) => state.role === "admin" || state.role === "approver",
      isAuthenticated: (state) => Boolean(state.token && state.accountId > 0 && state.identityId > 0 && state.role),
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
        this.sessionMode = null
        this.sessionInstanceId = ""
        doc.clearAccessSession()
      },
      apply(token: string, user: PlatformUser, mode: SessionMode = "refresh", sessionInstanceId?: string) {
        clearLegacyPersistence()
        const instanceId = isSessionInstanceId(sessionInstanceId)
          ? sessionInstanceId
          : (doc.getSessionInstanceId() ?? createSessionInstanceId())
        this.token = token
        this.accountId = user.account_id
        this.identityId = user.identity_id
        this.providerCode = user.provider_code
        this.username = user.username
        this.displayName = user.display_name
        this.dept = user.dept
        this.role = user.role
        this.sessionMode = mode === "access_only" ? "access_only" : "refresh"
        this.sessionInstanceId = instanceId
        doc.setAccessSession(token, user, this.sessionMode, instanceId)
      },
      clear() {
        try {
          doc.invalidateGeneration()
        } finally {
          this.resetIdentity()
          doc.clearRefreshTabBinding()
          try {
            window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
          } finally {
            clearLegacyPersistence()
          }
        }
      },
      /**
       * 仅当当前内存会话仍是预期实例/代际时清理。
       * 不匹配说明已有更新会话：返回 false，不广播、不跳转。
       */
      clearIfCurrent(
        expectedInstance: string | null,
        expectedGeneration: number,
        options: { broadcast?: boolean } = {},
      ): boolean {
        if (!isSessionInstanceId(expectedInstance)) return false
        if (doc.getSessionInstanceId() !== expectedInstance && this.sessionInstanceId !== expectedInstance) {
          return false
        }
        if (doc.generation !== expectedGeneration) return false
        this.clear()
        if (options.broadcast !== false) {
          doc.broadcastRetired(expectedInstance)
        }
        return true
      },
      clearAllTabs() {
        const instance =
          doc.getSessionInstanceId() ?? (isSessionInstanceId(this.sessionInstanceId) ? this.sessionInstanceId : null)
        this.clearIfCurrent(instance, doc.generation)
      },
      applyRemoteSessionRetired(raw: unknown): boolean {
        const message = parseSessionRetiredMessage(raw)
        if (!message) return false
        if (this.sessionInstanceId !== message.target_instance_id) return false
        if (!doc.rememberEventId(message.event_id)) return false
        this.clear()
        return true
      },
      applyStorageSessionSignal(event: { key?: string | null; newValue?: string | null }): boolean {
        return applyIncomingSessionSignal(this, event)
      },
      restore() {
        doc.bootstrapLegacyAccessSession()
        clearLegacyPersistence()
        const memoryToken = doc.getAccessToken()
        const memoryUser = doc.getSessionUser()
        if (memoryToken && memoryUser && isPlatformUser(memoryUser)) {
          this.apply(memoryToken, memoryUser, doc.sessionMode ?? "refresh", doc.getSessionInstanceId() ?? undefined)
          return
        }
        this.resetIdentity()
      },
      async restoreFromCookie(): Promise<boolean> {
        if (this.token) return true
        if (isAccessOnlySessionMode() || this.sessionMode === "access_only") return false
        const origin = doc.captureOrigin()
        try {
          return await doc.withSessionGeneration({ origin }, async ({ isLive, signal }) => {
            if (this.token) return true
            const result = await refreshRequest(signal)
            if (!isLive()) return false
            const instance = doc.getSessionInstanceId() ?? doc.readPublishedInstance() ?? createSessionInstanceId()
            this.apply(result.token, result.user, result.session_mode, instance)
            return true
          })
        } catch (error) {
          if (isSessionAbort(error)) return false
          if (error instanceof AuthApiError && error.code === "AUTH_REAUTH_REQUIRED") {
            window.dispatchEvent(new Event("sms:reauth-required"))
          }
          this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
          return false
        }
      },
      async revalidateOnResume(): Promise<boolean> {
        const origin = doc.captureOrigin()
        if (isAccessOnlySessionMode() || this.sessionMode === "access_only") {
          this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
          return false
        }
        try {
          return await doc.withSessionGeneration({ invalidateFirst: true, origin }, async ({ isLive, signal }) => {
            const result = await refreshRequest(signal)
            if (!isLive()) return false
            this.apply(
              result.token,
              result.user,
              result.session_mode,
              doc.getSessionInstanceId() ?? origin.sessionInstanceId ?? createSessionInstanceId(),
            )
            return true
          })
        } catch (error) {
          if (isSessionAbort(error)) return false
          if (error instanceof AuthApiError && error.code === "AUTH_REAUTH_REQUIRED") {
            window.dispatchEvent(new Event("sms:reauth-required"))
          }
          this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
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
        { nextAction: "authenticated" } | { nextAction: "change_password"; changeToken: string; expiresAt: number }
      > {
        const origin = doc.captureOrigin()
        const replacedInstance =
          doc.getSessionInstanceId() ??
          (isSessionInstanceId(this.sessionInstanceId) ? this.sessionInstanceId : null) ??
          doc.readPublishedInstance()
        try {
          return await doc.withSessionGeneration({ invalidateFirst: true, origin }, async ({ isLive, signal }) => {
            const response = await loginRequest(providerCode, username, password, signal)
            if (!isLive()) throw new SessionGenerationStaleError()
            if ("next_action" in response) {
              this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
              return {
                nextAction: "change_password",
                changeToken: response.change_token,
                expiresAt: Date.now() + response.expires_in * 1000,
              }
            }
            const mode =
              response.session_mode === "access_only" || response.session_mode === "refresh"
                ? response.session_mode
                : detectSessionMode()
            const nextInstance = createSessionInstanceId()
            this.apply(response.token, response.user, mode, nextInstance)
            if (replacedInstance && replacedInstance !== nextInstance) {
              doc.broadcastRetired(replacedInstance)
            }
            return { nextAction: "authenticated" }
          })
        } catch (error) {
          if (error instanceof SessionGenerationStaleError) {
            this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
          }
          throw error
        }
      },
      async changePassword(currentPassword: string, newPassword: string) {
        if (!this.token || this.providerCode !== "local") {
          throw new Error("仅已登录的本地账号可修改密码")
        }
        const origin = doc.captureOrigin()
        try {
          await passwordChangeRequest(this.token, currentPassword, newPassword)
        } catch (error) {
          if (error instanceof AuthApiError && error.code === "AUTH_CONTEXT_CHANGED") {
            this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
            if (!this.isAuthenticated) window.dispatchEvent(new Event("sms:unauthorized"))
          }
          throw error
        }
        this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration, { broadcast: false })
      },
      async logout(): Promise<SessionLogoutResult> {
        const origin = doc.captureOrigin()
        const token = this.token
        let networkError: unknown = null
        try {
          if (token) {
            await doc.withSessionGeneration({ invalidateFirst: true, origin }, async ({ signal }) => {
              await logoutRequest(token, signal)
            })
          }
        } catch (error) {
          if (!isSessionAbort(error)) networkError = error
        }
        const cleared = this.clearIfCurrent(origin.sessionInstanceId, origin.localGeneration)
        if (networkError) throw networkError
        return { cleared }
      },
    },
  })
  const boundStores = new WeakSet<ReturnType<typeof useStore>>()
  return Object.assign((...args: Parameters<typeof useStore>) => {
    const store = useStore(...args)
    if (!boundStores.has(store)) {
      boundStores.add(store)
      const unsubscribe = doc.onAccessSessionCleared(() => store.resetIdentity())
      const dispose = store.$dispose.bind(store)
      store.$dispose = () => {
        unsubscribe()
        boundStores.delete(store)
        dispose()
      }
    }
    return store
  }, useStore)
}

export const useSessionStore = createSessionStore()
