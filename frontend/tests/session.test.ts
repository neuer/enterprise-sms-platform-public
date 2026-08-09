import { createPinia, setActivePinia } from "pinia"
import { beforeEach, vi } from "vitest"

import { clearAccessSession } from "../src/api/sessionTokens"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

const admin = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "admin",
  display_name: "平台管理员",
  dept: "平台部",
  role: "admin" as const,
}

describe("Provider 与 JWT 会话", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearAccessSession()
    vi.unstubAllGlobals()
    setActivePinia(createPinia())
  })

  it("读取登录页可用认证源", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response([
        { code: "local", name: "本地账号", auth_flow: "password" },
        { code: "ad", name: "AD 账号", auth_flow: "password" },
      ]),
    )
    vi.stubGlobal("fetch", fetch)
    const session = useSessionStore()

    await session.loadProviders()

    expect(fetch).toHaveBeenCalledWith("/api/v1/web/auth/providers", {
      headers: { Accept: "application/json" },
    })
    expect(session.providers.map((item) => item.code)).toEqual(["local", "ad"])
  })

  it("登录后保存稳定账号摘要与 Bearer token", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({
        token: "jwt-token",
        expires_in: 900,
        refresh_expires_in: 604800,
        user: admin,
      }),
    )
    vi.stubGlobal("fetch", fetch)
    const session = useSessionStore()

    const next = await session.login("local", "admin", "Temp@Password123")

    expect(next).toEqual({ nextAction: "authenticated" })
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/auth/login",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_code: "local",
          username: "admin",
          password: "Temp@Password123",
        }),
      }),
    )
    expect(session.isAuthenticated).toBe(true)
    expect(session.accountId).toBe(8)
    expect(session.providerCode).toBe("local")
    expect(session.roleLabel).toBe("管理员")
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(localStorage.getItem("sms_token")).toBeNull()
    expect(localStorage.getItem("sms_user")).toBeNull()
  })

  it("首次登录只把 change token 返回给调用组件且不写任何浏览器存储", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          change_token: "change-once",
          expires_in: 600,
          next_action: "change_password",
        }),
      ),
    )
    const session = useSessionStore()

    const next = await session.login("local", "admin", "Temp@Password123")

    expect(next).toEqual({
      nextAction: "change_password",
      changeToken: "change-once",
      expiresAt: expect.any(Number),
    })
    expect(session.isAuthenticated).toBe(false)
    expect("changeToken" in session.$state).toBe(false)
    expect(JSON.stringify(sessionStorage)).not.toContain("change-once")
    expect(JSON.stringify(localStorage)).not.toContain("change-once")
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(localStorage.length).toBe(0)
  })

  it("恢复时丢弃旧版持久化改密令牌且只恢复完整访问会话", () => {
    sessionStorage.setItem("sms_change_token", "change-once")
    sessionStorage.setItem("sms_change_token_expires_at", String(Date.now() + 600_000))
    sessionStorage.setItem("sms_token", "must-not-authenticate")
    sessionStorage.setItem("sms_refresh_token", "must-not-refresh")
    sessionStorage.setItem("sms_user", JSON.stringify(admin))
    localStorage.setItem("sms_token", "legacy")
    const session = useSessionStore()

    session.restore()

    expect(session.isAuthenticated).toBe(true)
    expect(sessionStorage.getItem("sms_change_token")).toBeNull()
    expect(sessionStorage.getItem("sms_change_token_expires_at")).toBeNull()
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(localStorage.length).toBe(0)
  })

  it("恢复时丢弃已经过期的改密令牌", () => {
    sessionStorage.setItem("sms_change_token", "stale-change")
    sessionStorage.setItem("sms_change_token_expires_at", String(Date.now() - 1))
    const session = useSessionStore()

    session.restore()

    expect(sessionStorage.getItem("sms_change_token")).toBeNull()
    expect(sessionStorage.getItem("sms_change_token_expires_at")).toBeNull()
  })

  it("恢复时丢弃没有时限的旧版改密令牌", () => {
    sessionStorage.setItem("sms_change_token", "legacy-change")
    const session = useSessionStore()

    session.restore()

    expect(sessionStorage.getItem("sms_change_token")).toBeNull()
  })

  it("登出接口失败也清除访问与改密会话", async () => {
    sessionStorage.setItem("sms_token", "jwt-token")
    sessionStorage.setItem("sms_refresh_token", "refresh-token")
    sessionStorage.setItem("sms_user", JSON.stringify(admin))
    sessionStorage.setItem("sms_change_token", "stale-change")
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")))
    const session = useSessionStore()
    session.restore()

    await expect(session.logout()).rejects.toThrow("offline")

    expect(session.isAuthenticated).toBe(false)
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.length).toBe(0)
  })
})
