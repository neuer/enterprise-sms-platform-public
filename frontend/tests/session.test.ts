import { createPinia, setActivePinia } from "pinia"
import { beforeEach, vi } from "vitest"

import { authorization, authorizedFetch } from "../src/api/webMessages"
import {
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  REFRESH_TAB_ID_KEY,
  resetAccessSessionModule,
} from "../src/api/sessionTokens"
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

const operator = {
  account_id: 9,
  identity_id: 19,
  provider_code: "local",
  username: "operator01",
  display_name: "操作员",
  dept: "业务部",
  role: "operator" as const,
}

function seedLegacyAccess(token = "legacy-access"): void {
  sessionStorage.setItem("sms_token", token)
  sessionStorage.setItem("sms_user", JSON.stringify(admin))
}

function throwSecurityError(): never {
  throw new DOMException("restricted", "SecurityError")
}

function stubRemoveItemThrows(): ReturnType<typeof vi.spyOn> {
  return vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
    throwSecurityError()
  })
}

function trapBearerStorageWrites(): ReturnType<typeof vi.spyOn> {
  const nativeSetItem = Storage.prototype.setItem
  return vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
    this: Storage,
    key: string,
    value: string,
  ) {
    if (key === "sms_token" || key === "sms_user" || key === "sms_refresh_token") {
      throw new Error(`unexpected storage write ${key}`)
    }
    return nativeSetItem.call(this, key, value)
  })
}

describe("Provider 与 JWT 会话", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    clearRefreshTabBinding()
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
    const storageSignal = vi.spyOn(Storage.prototype, "setItem")
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
      }),
    )
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toMatchObject({
      provider_code: "local",
      username: "admin",
      password: "Temp@Password123",
      tab_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    })
    expect(session.isAuthenticated).toBe(true)
    expect(session.accountId).toBe(8)
    expect(session.providerCode).toBe("local")
    expect(session.roleLabel).toBe("管理员")
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(sessionStorage.getItem(REFRESH_TAB_ID_KEY)).toMatch(/^[0-9a-f]{32}$/)
    expect(localStorage.getItem("sms_token")).toBeNull()
    expect(localStorage.getItem("sms_user")).toBeNull()
    expect(storageSignal).toHaveBeenCalledWith("sms_session_clear", expect.any(String))
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

  it.each([
    ["localStorage", "getItem"],
    ["localStorage", "setItem"],
    ["localStorage", "removeItem"],
    ["sessionStorage", "getItem"],
    ["sessionStorage", "setItem"],
    ["sessionStorage", "removeItem"],
  ] as const)("window.%s.%s 抛错时本页 Access Token 仍立即清空", (storageName, method) => {
    const session = useSessionStore()
    session.apply("jwt-token", admin)
    const storage = window[storageName]
    const spy = vi.spyOn(storage, method).mockImplementation(() => {
      throw new DOMException("restricted", "SecurityError")
    })
    try {
      expect(() => session.clearAllTabs()).not.toThrow()
      expect(session.isAuthenticated).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(session.accountId).toBe(0)
      expect(session.identityId).toBe(0)
    } finally {
      spy.mockRestore()
    }
  })

  it.each(["localStorage", "sessionStorage"] as const)(
    "window.%s getter 抛错时本页 Access Token 仍立即清空",
    (storageName) => {
      const session = useSessionStore()
      session.apply("jwt-token", admin)
      const spy = vi.spyOn(window, storageName, "get").mockImplementation(() => {
        throw new DOMException("restricted", "SecurityError")
      })
      try {
        expect(() => session.clearAllTabs()).not.toThrow()
        expect(session.isAuthenticated).toBe(false)
        expect(getAccessToken()).toBeNull()
        expect(session.accountId).toBe(0)
        expect(session.identityId).toBe(0)
      } finally {
        spy.mockRestore()
      }
    },
  )

  it("clear 与 logout 连续执行多次不因 Storage 清理抛错", async () => {
    const session = useSessionStore()
    session.apply("jwt-token", admin)
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")))
    expect(() => {
      session.clear()
      session.clear()
      session.clearAllTabs()
    }).not.toThrow()
    session.apply("jwt-token", admin)
    await expect(session.logout()).rejects.toThrow("offline")
    await expect(session.logout()).resolves.toBeUndefined()
    expect(session.isAuthenticated).toBe(false)
    expect(getAccessToken()).toBeNull()
  })

  it("BFCache 恢复时必须向服务端重新验证，失败则清除全部标签页", async () => {
    const storageSignal = vi.spyOn(Storage.prototype, "setItem")
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, "a".repeat(32))
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "UNAUTHORIZED" }, 401)))
    const session = useSessionStore()
    session.apply("jwt-token", admin)

    await expect(session.revalidateOnResume()).resolves.toBe(false)

    expect(session.isAuthenticated).toBe(false)
    expect(storageSignal).toHaveBeenCalledWith("sms_session_clear", expect.any(String))
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

describe("历史 Storage 残留不得再次导入 Access Token", () => {
  // TEST-MANUAL SMK-05 / SMK-05a：真实浏览器可读取但禁止删除 Storage 时，
  // 退出后本页不得再从 sms_token/sms_user 恢复旧 Bearer。
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    clearRefreshTabBinding()
    vi.unstubAllGlobals()
    setActivePinia(createPinia())
  })

  it("getItem 成功而 removeItem 抛错时只导入一次", () => {
    seedLegacyAccess()
    const remove = stubRemoveItemThrows()
    const getItem = vi.spyOn(Storage.prototype, "getItem")
    try {
      expect(getAccessToken()).toBe("legacy-access")
      expect(getSessionUser()).toEqual(admin)
      const tokenReads = getItem.mock.calls.filter(([key]) => key === "sms_token").length
      expect(tokenReads).toBeGreaterThan(0)
      expect(getAccessToken()).toBe("legacy-access")
      expect(getSessionUser()).toEqual(admin)
      expect(getItem.mock.calls.filter(([key]) => key === "sms_token")).toHaveLength(tokenReads)
      expect(sessionStorage.getItem("sms_token")).toBe("legacy-access")
    } finally {
      getItem.mockRestore()
      remove.mockRestore()
    }
  })

  it("clear 后 getter 不得返回残留 Token", () => {
    seedLegacyAccess()
    const remove = stubRemoveItemThrows()
    const session = useSessionStore()
    session.restore()
    expect(getAccessToken()).toBe("legacy-access")
    try {
      expect(() => session.clear()).not.toThrow()
      expect(getAccessToken()).toBeNull()
      expect(getSessionUser()).toBeNull()
      expect(session.isAuthenticated).toBe(false)
      expect(sessionStorage.getItem("sms_token")).toBe("legacy-access")
      expect(sessionStorage.getItem("sms_user")).toBe(JSON.stringify(admin))
    } finally {
      remove.mockRestore()
    }
  })

  it("clear 后 authorized fetch 不得附带旧 Authorization", async () => {
    seedLegacyAccess()
    const remove = stubRemoveItemThrows()
    const session = useSessionStore()
    session.restore()
    expect(authorization()).toEqual({ Authorization: "Bearer legacy-access" })
    expect(() => session.clear()).not.toThrow()
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetch)
    try {
      expect(authorization()).toEqual({})
      expect(getAccessToken()).toBeNull()
      await authorizedFetch("/api/v1/web/reports/dashboard", { method: "GET" })
      expect(fetch).toHaveBeenCalledOnce()
      expect(fetch.mock.calls[0][1].headers.Authorization).toBeUndefined()
    } finally {
      remove.mockRestore()
    }
  })

  it.each([
    ["成功", response({}, 204), false],
    ["失败", new Error("offline"), true],
  ] as const)("服务端 logout %s 后本地清理单调且不得再导入", async (_label, logoutResult, rejects) => {
    seedLegacyAccess()
    const remove = stubRemoveItemThrows()
    const setItem = trapBearerStorageWrites()
    const session = useSessionStore()
    session.restore()
    if (logoutResult instanceof Error) {
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(logoutResult))
    } else {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(logoutResult))
    }
    try {
      if (rejects) await expect(session.logout()).rejects.toThrow("offline")
      else await expect(session.logout()).resolves.toBeUndefined()
      expect(session.isAuthenticated).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(getSessionUser()).toBeNull()
      expect(getAccessToken()).toBeNull()
      expect(sessionStorage.getItem("sms_token")).toBe("legacy-access")
    } finally {
      setItem.mockRestore()
      remove.mockRestore()
    }
  })

  it("BFCache 恢复不得重新执行 legacy migration", async () => {
    seedLegacyAccess()
    const remove = stubRemoveItemThrows()
    const session = useSessionStore()
    session.restore()
    expect(() => session.clear()).not.toThrow()
    const getItem = vi.spyOn(Storage.prototype, "getItem")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "UNAUTHORIZED" }, 401)))
    try {
      await expect(session.revalidateOnResume()).resolves.toBe(false)
      session.restore()
      expect(getAccessToken()).toBeNull()
      expect(getSessionUser()).toBeNull()
      expect(session.isAuthenticated).toBe(false)
      expect(getItem.mock.calls.filter(([key]) => key === "sms_token")).toHaveLength(0)
      expect(sessionStorage.getItem("sms_token")).toBe("legacy-access")
    } finally {
      getItem.mockRestore()
      remove.mockRestore()
    }
  })

  it("新主体登录后残留 Storage 不得覆盖内存会话", () => {
    const session = useSessionStore()
    seedLegacyAccess("old-access")
    const remove = stubRemoveItemThrows()
    const setItem = trapBearerStorageWrites()
    try {
      session.apply("new-access", operator)
      expect(getAccessToken()).toBe("new-access")
      expect(getSessionUser()).toEqual(operator)
      expect(session.username).toBe("operator01")
      session.restore()
      expect(getAccessToken()).toBe("new-access")
      expect(getSessionUser()).toEqual(operator)
      expect(session.username).toBe("operator01")
      expect(sessionStorage.getItem("sms_token")).toBe("old-access")
    } finally {
      setItem.mockRestore()
      remove.mockRestore()
    }
  })

  it("先导入旧主体再登录新主体时残留 Storage 不得回流", () => {
    seedLegacyAccess("old-access")
    const remove = stubRemoveItemThrows()
    try {
      expect(getAccessToken()).toBe("old-access")
      const session = useSessionStore()
      session.apply("new-access", operator)
      expect(getAccessToken()).toBe("new-access")
      session.restore()
      expect(getAccessToken()).toBe("new-access")
      expect(getSessionUser()).toEqual(operator)
      expect(sessionStorage.getItem("sms_token")).toBe("old-access")
    } finally {
      remove.mockRestore()
    }
  })

  it.each([
    ["localStorage", "getItem"],
    ["localStorage", "setItem"],
    ["localStorage", "removeItem"],
    ["sessionStorage", "getItem"],
    ["sessionStorage", "setItem"],
    ["sessionStorage", "removeItem"],
  ] as const)("残留会话下 window.%s.%s 失败后 clear 仍单调关闭", (storageName, method) => {
    seedLegacyAccess()
    const session = useSessionStore()
    session.restore()
    expect(getAccessToken()).toBe("legacy-access")
    const storage = window[storageName]
    const spy = vi.spyOn(storage, method).mockImplementation(() => {
      throwSecurityError()
    })
    try {
      expect(() => session.clearAllTabs()).not.toThrow()
      expect(session.isAuthenticated).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(getSessionUser()).toBeNull()
      expect(authorization()).toEqual({})
    } finally {
      spy.mockRestore()
    }
  })

  it.each(["localStorage", "sessionStorage"] as const)(
    "残留会话下 window.%s getter 失败后 clear 仍单调关闭",
    (storageName) => {
      seedLegacyAccess()
      const session = useSessionStore()
      session.restore()
      expect(getAccessToken()).toBe("legacy-access")
      const spy = vi.spyOn(window, storageName, "get").mockImplementation(() => {
        throwSecurityError()
      })
      try {
        expect(() => session.clearAllTabs()).not.toThrow()
        expect(session.isAuthenticated).toBe(false)
        expect(getAccessToken()).toBeNull()
        expect(getSessionUser()).toBeNull()
      } finally {
        spy.mockRestore()
      }
    },
  )
})
