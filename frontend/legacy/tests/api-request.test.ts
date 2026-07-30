import { beforeEach, vi } from "vitest"

import { apiRequest } from "../src/api/webMessages"
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

describe("统一 API 请求", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.unstubAllGlobals()
  })

  it("401 时清除持久会话并广播未授权事件", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_refresh_token", "stale-refresh")
    sessionStorage.setItem("sms_user", "{}")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
    vi.stubGlobal("fetch", fetch)
    const unauthorized = vi.fn()
    window.addEventListener("sms:unauthorized", unauthorized, { once: true })

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow("UNAUTHORIZED")

    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).toHaveBeenCalledOnce()
  })

  it("访问令牌失效时轮换 refresh token、更新用户上下文并重放请求", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_refresh_token", "refresh-1")
    sessionStorage.setItem("sms_user", "{}")
    const user = {
      account_id: 8,
      identity_id: 18,
      provider_code: "local",
      username: "admin",
      display_name: "管理员",
      dept: "新部门",
      role: "approver",
    }
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(
        response(
          {
            token: "access-2",
            refresh_token: "refresh-2",
            expires_in: 900,
            refresh_expires_in: 604800,
            user,
          },
          200,
        ),
      )
      .mockResolvedValueOnce(response({ total: 7 }, 200))
    vi.stubGlobal("fetch", fetch)
    const refreshed = vi.fn()
    window.addEventListener("sms:session-refreshed", refreshed, { once: true })

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).resolves.toEqual({
      total: 7,
    })

    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/auth/refresh")
    expect(fetch.mock.calls[2][1].headers).toMatchObject({
      Authorization: "Bearer access-2",
    })
    expect(sessionStorage.getItem("sms_refresh_token")).toBe("refresh-2")
    expect(refreshed).toHaveBeenCalledOnce()
  })

  it("refresh 状态服务故障时保留会话并显式返回 503", async () => {
    sessionStorage.setItem("sms_token", "expired")
    sessionStorage.setItem("sms_refresh_token", "refresh-1")
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

    await expect(apiRequest("/reports/dashboard", { method: "GET" })).rejects.toThrow(
      "会话权威状态暂不可用",
    )

    expect(sessionStorage.getItem("sms_token")).toBe("expired")
    expect(sessionStorage.getItem("sms_refresh_token")).toBe("refresh-1")
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
})
