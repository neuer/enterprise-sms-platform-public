import { afterEach, beforeEach, vi } from "vitest"

import { apiRequest, authorizedFetch } from "../src/api/webMessages"
import {
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  REFRESH_TAB_ID_KEY,
  resetAccessSessionModule,
  setAccessSession,
} from "../src/api/sessionTokens"
import {
  createLocalUser,
  listUsers,
  resetLocalPassword,
  revokeUserSessions,
  updateUserRole,
  updateUserStatus,
} from "../src/api/users"

function response(body: unknown, status: number) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function invalidJsonResponse(status: number) {
  return new Response("{", {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

describe("统一 API 请求", () => {
  const tabId = "d".repeat(32)
  const unauthorizedListeners: EventListener[] = []

  function watchUnauthorized() {
    const listener = vi.fn()
    unauthorizedListeners.push(listener)
    window.addEventListener("sms:unauthorized", listener)
    return listener
  }

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    clearRefreshTabBinding()
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, tabId)
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    for (const listener of unauthorizedListeners) {
      window.removeEventListener("sms:unauthorized", listener)
    }
    unauthorizedListeners.length = 0
  })

  it("Storage 删除失败且 401 清理后不得再附带旧 Authorization", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", JSON.stringify({
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "admin",
      display_name: "管理员",
      dept: "平台部",
      role: "admin",
    }))
    const remove = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("restricted", "SecurityError")
    })
    const unauthorized = watchUnauthorized()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(response({ ok: true }, 200))
    vi.stubGlobal("fetch", fetch)
    try {
      await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow("UNAUTHORIZED")
      expect(unauthorized).toHaveBeenCalledOnce()
      expect(getAccessToken()).toBeNull()
      await authorizedFetch("/api/v1/web/reports/dashboard", { method: "GET" })
      const replayHeaders = fetch.mock.calls.at(-1)?.[1] as RequestInit
      expect((replayHeaders.headers as Record<string, string>).Authorization).toBeUndefined()
    } finally {
      remove.mockRestore()
    }
  })

  it("401 时清除持久会话并广播未授权事件", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", "{}")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
    vi.stubGlobal("fetch", fetch)
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow("UNAUTHORIZED")

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it("访问令牌失效时单次轮换 refresh token 并重放原请求", async () => {
    const refreshed = watchUnauthorized()
    const sessionRefreshed = vi.fn()
    window.addEventListener("sms:session-refreshed", sessionRefreshed, { once: true })
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", "{}")
    const updatedUser = {
      account_id: 8,
      identity_id: 18,
      provider_code: "ad",
      username: "operator01",
      display_name: "目录操作员",
      dept: "调整后部门",
      role: "approver",
    }
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(
        response(
          {
            token: "access-2",
            expires_in: 900,
            refresh_expires_in: 604800,
            user: updatedUser,
          },
          200,
        ),
      )
      .mockResolvedValueOnce(response({ total: 7 }, 200))
    vi.stubGlobal("fetch", fetch)

    await expect(apiRequest<{ total: number }>("/reports/dashboard", { method: "GET" })).resolves
      .toEqual({ total: 7 })

    expect(fetch).toHaveBeenCalledTimes(3)
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/auth/refresh")
    expect(JSON.parse(String(fetch.mock.calls[1][1].body))).toEqual({ tab_id: tabId })
    expect(fetch.mock.calls[2][1].headers).toMatchObject({
      Authorization: "Bearer access-2",
    })
    expect(getAccessToken()).toBe("access-2")
    expect(getSessionUser()).toEqual(updatedUser)
    expect(sessionRefreshed).toHaveBeenCalledOnce()
    expect(refreshed).not.toHaveBeenCalled()
  })

  it("并发 401 共享一次 refresh，避免重复消费单次令牌", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", "{}")
    const user = {
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "admin",
      display_name: "管理员",
      dept: "平台部",
      role: "admin",
    }
    const fetch = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/api/v1/web/auth/refresh") {
        return Promise.resolve(
          response(
            {
              token: "access-2",
              expires_in: 900,
              refresh_expires_in: 604800,
              user,
            },
            200,
          ),
        )
      }
      const authorization = (init?.headers as Record<string, string>).Authorization
      return Promise.resolve(
        authorization === "Bearer expired"
          ? response({ code: "UNAUTHORIZED" }, 401)
          : response({ ok: true }, 200),
      )
    })
    vi.stubGlobal("fetch", fetch)

    await Promise.all([
      apiRequest("/reports/dashboard", { method: "GET" }),
      apiRequest("/reports/dashboard", { method: "GET" }),
    ])

    expect(
      fetch.mock.calls.filter(([url]) => url === "/api/v1/web/auth/refresh"),
    ).toHaveLength(1)
  })

  it("refresh 返回不同稳定主体时清空旧标签且不重放原请求", async () => {
    const originalUser = {
      account_id: 7,
      identity_id: 17,
      provider_code: "local",
      username: "operator",
      display_name: "操作员",
      dept: "业务部",
      role: "operator" as const,
    }
    const replacementUser = {
      ...originalUser,
      account_id: 8,
      identity_id: 18,
      username: "admin",
      role: "admin" as const,
    }
    setAccessSession("expired", originalUser)
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(
        response(
          {
            token: "admin-access",
            expires_in: 900,
            refresh_expires_in: 604800,
            user: replacementUser,
          },
          200,
        ),
      )
    vi.stubGlobal("fetch", fetch)
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow(
      "UNAUTHORIZED",
    )

    expect(fetch).toHaveBeenCalledTimes(2)
    expect(getAccessToken()).toBeNull()
    expect(getSessionUser()).toBeNull()
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it("refresh 状态服务故障时保留会话并显式返回 503", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", "{}")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(
        response(
          {
            code: "AUTH_SESSION_UNAVAILABLE",
            message: "会话权威状态暂不可用",
          },
          503,
        ),
      )
    vi.stubGlobal("fetch", fetch)
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow(
      "会话权威状态暂不可用",
    )

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it.each(["STEP_UP_REQUIRED", "STEP_UP_EXPIRED"])("%s 时保留当前会话且不广播未授权事件", async (code) => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{}")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code }, 401)))
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/admin/vendor-test/step-up", { method: "POST" })).rejects.toThrow(code)

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it.each([
    ["缺少错误码", response({}, 401)],
    ["错误响应无法解析", invalidJsonResponse(401)],
  ])("401 %s 时保留会话且不广播未授权事件", async (_scenario, rejectedResponse) => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{}")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(rejectedResponse))
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow()

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it("401 响应 clone 失败时保留会话且不广播未授权事件", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{}")
    const rejectedResponse = response({ code: "UNAUTHORIZED" }, 401)
    vi.spyOn(rejectedResponse, "clone").mockImplementation(() => {
      throw new TypeError("clone failed")
    })
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(rejectedResponse))
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow("UNAUTHORIZED")

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it("明确 ACCOUNT_LOCKED 时清除会话并广播未授权事件", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{}")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "ACCOUNT_LOCKED" }, 423)))
    const unauthorized = watchUnauthorized()

    await expect(apiRequest("/admin/vendor-test/step-up", { method: "POST" })).rejects.toThrow(
      "ACCOUNT_LOCKED",
    )

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it("用户管理只使用 account_id 路由并完整传递 Provider 状态过滤", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    const managed = {
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "operator.local",
      display_name: "本地操作员",
      dept: "业务一部",
      role: "operator",
      role_override: true,
      status: 1,
      identity_status: 1,
      credential_status: "must_change",
      source_groups: [],
      sync_status: "local",
      last_synced_at: null,
      last_login_at: null,
    }
    const fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("/admin/users?")) {
        return Promise.resolve(response({ items: [managed], total: 1, page: 2, page_size: 20 }, 200))
      }
      if (url.endsWith("/sessions/revoke")) return Promise.resolve(response(undefined, 204))
      return Promise.resolve(response(managed, 200))
    })
    vi.stubGlobal("fetch", fetch)

    const page = await listUsers({
      keyword: "操作",
      providerCode: "local",
      role: "operator",
      status: 1,
      page: 2,
      pageSize: 20,
    })
    const created = await createLocalUser({
      username: "new.user",
      display_name: "新用户",
      dept: "业务一部",
      role: "viewer",
      temporary_password: "Temporary@123",
    })
    await updateUserRole(8, "approver", true)
    await updateUserStatus(8, 0)
    await resetLocalPassword(8, "Reset@Password123")
    await revokeUserSessions(8)

    const calls = fetch.mock.calls
    expect(calls[0][0]).toContain("provider_code=local")
    expect(calls[0][0]).toContain("status=1")
    expect(calls[1][0]).toBe("/api/v1/web/admin/users/local")
    expect(calls[2][0]).toBe("/api/v1/web/admin/users/8/role")
    expect(calls[3][0]).toBe("/api/v1/web/admin/users/8/status")
    expect(calls[4][0]).toBe("/api/v1/web/admin/users/8/password/reset")
    expect(calls[5][0]).toBe("/api/v1/web/admin/users/8/sessions/revoke")
    expect(JSON.parse(String(calls[1][1].body)).temporary_password).toBe("Temporary@123")
    expect(JSON.stringify(created)).not.toContain("temporary_password")
    expect(page.items[0].account_id).toBe(8)
  })

  it.each([
    "https://evil.example/api/v1/web/reports/dashboard",
    "//evil.example/api/v1/web/reports/dashboard",
    "javascript:alert(1)",
    "data:text/plain,hello",
  ])("授权请求拒绝非同源目标 %s 且不调用 fetch", async (url) => {
    const fetch = vi.fn()
    vi.stubGlobal("fetch", fetch)

    await expect(authorizedFetch(url, { method: "GET" })).rejects.toThrow(
      "授权请求必须与当前站点同源",
    )
    expect(fetch).not.toHaveBeenCalled()
  })

  it("请求超过默认总超时后以明确错误失败", async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("请求超时", "TimeoutError")),
          )
        }),
    )
    vi.stubGlobal("fetch", fetch)

    const pending = authorizedFetch("/api/v1/web/reports/dashboard", { method: "GET" })
    const assertion = expect(pending).rejects.toThrow("请求超时")
    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it("会话清理事件取消全部在途授权请求", async () => {
    const fetch = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("会话已清理", "AbortError")),
          )
        }),
    )
    vi.stubGlobal("fetch", fetch)

    const pending = authorizedFetch("/api/v1/web/reports/dashboard", { method: "GET" })
    const assertion = expect(pending).rejects.toThrow("会话已清理")
    window.dispatchEvent(new Event("sms:session-clearing"))
    await assertion
  })

  it("注销开始后丢弃仍在飞行的 refresh 结果", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", JSON.stringify({
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "admin",
      display_name: "管理员",
      dept: "平台部",
      role: "admin",
    }))
    setAccessSession("expired", {
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "admin",
      display_name: "管理员",
      dept: "平台部",
      role: "admin",
    })
    let releaseRefresh!: (value: Response) => void
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/web/auth/refresh") {
        return new Promise<Response>((resolve) => {
          releaseRefresh = resolve
          init?.signal?.addEventListener("abort", () =>
            resolve(response({ code: "UNAUTHORIZED" }, 401)),
          )
        })
      }
      return Promise.resolve(response({ code: "UNAUTHORIZED" }, 401))
    })
    vi.stubGlobal("fetch", fetch)

    const pending = apiRequest("/reports/dashboard", { method: "GET" })
    await vi.waitFor(() => {
      expect(typeof releaseRefresh).toBe("function")
    })
    window.dispatchEvent(new Event("sms:session-clearing"))
    releaseRefresh(
      response(
        {
          token: "access-should-discard",
          expires_in: 900,
          refresh_expires_in: 604800,
          user: {
            account_id: 8,
            identity_id: 18,
            provider_code: "local",
            username: "admin",
            display_name: "管理员",
            dept: "平台部",
            role: "admin",
          },
        },
        200,
      ),
    )
    await expect(pending).rejects.toThrow()
    expect(getAccessToken()).toBeNull()
  })

  it("刷新超时后释放 single-flight 并可再次发起", async () => {
    vi.useFakeTimers()
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_user", "{}")
    let refreshCalls = 0
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/web/auth/refresh") {
        refreshCalls += 1
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("刷新超时", "TimeoutError")),
          )
        })
      }
      return Promise.resolve(response({ code: "UNAUTHORIZED" }, 401))
    })
    vi.stubGlobal("fetch", fetch)

    const first = apiRequest("/reports/dashboard", { method: "GET" })
    const firstAssertion = expect(first).rejects.toThrow("会话权威状态暂不可用")
    await vi.advanceTimersByTimeAsync(10_000)
    await firstAssertion
    expect(refreshCalls).toBe(1)

    const second = apiRequest("/reports/dashboard", { method: "GET" })
    const secondAssertion = expect(second).rejects.toThrow("会话权威状态暂不可用")
    await vi.advanceTimersByTimeAsync(10_000)
    await secondAssertion
    expect(refreshCalls).toBe(2)
  })
})
