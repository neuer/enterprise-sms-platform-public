import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { authorizedBlob, authorizedFetch, authorizedJsonResult } from "../src/api/client"
import { defaultSessionDocument } from "../src/api/sessionDocument"
import { beginRefreshTabBinding, getAccessToken, resetAccessSessionModule } from "../src/api/sessionTokens"
import { useSessionStore } from "../src/stores/session"

const user = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "synthetic",
  display_name: "测试",
  dept: "测试",
  role: "admin" as const,
}
const surfaces = [
  { name: "json", run: () => authorizedJsonResult("/api/v1/web/test", {}) },
  { name: "raw", run: () => authorizedFetch("/api/v1/web/test", {}) },
  { name: "blob", run: () => authorizedBlob("/api/v1/web/test", {}) },
]
const terminal = [
  [401, "UNAUTHORIZED"],
  [401, "AUTH_REAUTH_REQUIRED"],
  [409, "AUTH_CONTEXT_CHANGED"],
  [423, "ACCOUNT_LOCKED"],
] as const
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}
let session: ReturnType<typeof useSessionStore>
beforeEach(() => {
  resetAccessSessionModule()
  setActivePinia(createPinia())
  session = useSessionStore()
  session.apply("initial", user, "refresh", "a".repeat(32))
  beginRefreshTabBinding()
})
afterEach(() => {
  session.$dispose()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe.each(surfaces)("$name 权威退出", ({ run, name }) => {
  it.each(terminal)("刷新重放后 %s %s 同步销毁真实 Pinia 与 Document", async (status, code) => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED" }, 401))
      .mockResolvedValueOnce(response({ token: "renewed", user, session_mode: "refresh", expires_in: 900 }))
      .mockResolvedValueOnce(response({ code }, status))
    vi.stubGlobal("fetch", fetch)
    const cleared = vi.fn()
    const unsubscribe = defaultSessionDocument.onAccessSessionCleared(cleared)
    const event = vi.fn()
    window.addEventListener("sms:session-clearing", event)
    try {
      if (name === "blob") await expect(run()).rejects.toMatchObject({ code })
      else expect(await run()).toMatchObject({ status })
      expect(fetch).toHaveBeenCalledTimes(3)
      expect(session.isAuthenticated).toBe(false)
      expect(session.token).toBe("")
      expect(session.accountId).toBe(0)
      expect(session.role).toBeNull()
      expect(getAccessToken()).toBeNull()
      expect(cleared).toHaveBeenCalledTimes(1)
      expect(event).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
      window.removeEventListener("sms:session-clearing", event)
    }
  })

  it.each(terminal.slice(1))("首次 %s %s 同样清理，无刷新", async (status, code) => {
    const fetch = vi.fn().mockResolvedValue(response({ code }, status))
    vi.stubGlobal("fetch", fetch)
    if (name === "blob") await expect(run()).rejects.toMatchObject({ code })
    else expect(await run()).toMatchObject({ status })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(session.isAuthenticated).toBe(false)
    expect(getAccessToken()).toBeNull()
  })

  it.each(terminal)("同实例已轮换 Token 的重放 %s %s 不再刷新", async (status, code) => {
    const fetch = vi
      .fn()
      .mockImplementationOnce(async () => {
        session.apply("rotated", user, "refresh", "a".repeat(32))
        return response({ code: "UNAUTHORIZED" }, 401)
      })
      .mockResolvedValueOnce(response({ code }, status))
    vi.stubGlobal("fetch", fetch)
    if (name === "blob") await expect(run()).rejects.toMatchObject({ code })
    else expect(await run()).toMatchObject({ status })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(session.isAuthenticated).toBe(false)
  })

  it.each(terminal)("旧请求迟到的 %s %s 不退出新会话", async (status, code) => {
    let resolve!: (value: Response) => void
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((done) => {
            resolve = done
          }),
      ),
    )
    const pending = run()
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" })
    defaultSessionDocument.invalidateGeneration()
    session.apply("new-login", user, "refresh", "b".repeat(32))
    resolve(response({ code }, status))
    await assertion
    expect(session.isAuthenticated).toBe(true)
    expect(session.token).toBe("new-login")
    expect(getAccessToken()).toBe("new-login")
  })

  it("权威状态暂不可用保留当前会话", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "AUTH_SESSION_UNAVAILABLE" }, 503)))
    if (name === "blob") await expect(run()).rejects.toMatchObject({ code: "AUTH_SESSION_UNAVAILABLE" })
    else expect(await run()).toMatchObject({ status: 503 })
    expect(session.isAuthenticated).toBe(true)
    expect(getAccessToken()).toBe("initial")
  })
})
