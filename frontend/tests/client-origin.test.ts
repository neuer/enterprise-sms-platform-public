import { afterEach, describe, expect, it, vi } from "vitest"
import { flushPromises } from "@vue/test-utils"

import { authorizedBlob, authorizedFetch, authorizedJsonResult } from "../src/api/client"
import { invalidateSessionGeneration } from "../src/api/sessionGeneration"
import { beginRefreshTabBinding, getAccessToken, setAccessSession } from "../src/api/sessionTokens"

const USER = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "operator01",
  display_name: "测试用户",
  dept: "研发部",
  role: "operator" as const,
}
const INITIAL = "a".repeat(32)
const NEXT = "b".repeat(32)
const PAYLOAD = JSON.stringify({ operation: "synthetic-business-change" })
const surfaces = [
  { name: "json", trigger: 2, run: () => authorizedJsonResult("/api/v1/web/test", { method: "POST", body: PAYLOAD }) },
  { name: "raw", trigger: 3, run: () => authorizedFetch("/api/v1/web/test", { method: "POST", body: PAYLOAD }) },
  { name: "blob", trigger: 2, run: () => authorizedBlob("/api/v1/web/test", { method: "POST", body: PAYLOAD }) },
]

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

function afterBody(status: number, trigger: number, work: () => void): Response {
  const value = response({ code: "UNAUTHORIZED" }, status)
  let reads = 0
  Object.defineProperty(value, "status", {
    get() {
      reads += 1
      if (reads === trigger) queueMicrotask(work)
      return status
    },
  })
  return value
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe.each(surfaces)("$name 请求来源", ({ run, trigger }) => {
  it.each([false, true])("新登录后不重放旧 POST，同账号=%s", async (sameAccount) => {
    setAccessSession("old-token", USER, "refresh", INITIAL)
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        afterBody(401, trigger, () => {
          invalidateSessionGeneration()
          setAccessSession(
            "new-token",
            sameAccount ? USER : { ...USER, account_id: 9, identity_id: 19 },
            "refresh",
            NEXT,
          )
        }),
      )
      .mockResolvedValue(response({ ok: true }))
    vi.stubGlobal("fetch", fetch)
    await expect(run()).rejects.toMatchObject({ name: "AbortError" })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1].body).toBe(PAYLOAD)
    expect(getAccessToken()).toBe("new-token")
  })

  it("同实例正常 Token 轮换只重试一次", async () => {
    setAccessSession("old-token", USER, "refresh", INITIAL)
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        afterBody(401, trigger, () => {
          setAccessSession("rotated-token", USER, "refresh", INITIAL)
        }),
      )
      .mockResolvedValue(response({ ok: true }))
    vi.stubGlobal("fetch", fetch)
    await run()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[1][1].headers.Authorization).toBe("Bearer rotated-token")
    expect(fetch.mock.calls[1][1].body).toBe(PAYLOAD)
  })

  it("等待 Refresh 时切换实例不会继续业务传输", async () => {
    setAccessSession("old-token", USER, "refresh", INITIAL)
    beginRefreshTabBinding()
    let release!: (value: Response) => void
    const fetch = vi.fn((url: string) =>
      url.includes("/auth/refresh")
        ? new Promise<Response>((resolve) => {
            release = resolve
          })
        : Promise.resolve(response({ code: "UNAUTHORIZED" }, 401)),
    )
    vi.stubGlobal("fetch", fetch)
    const pending = run()
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" })
    await flushPromises()
    expect(release).toBeTypeOf("function")
    invalidateSessionGeneration()
    setAccessSession("new-token", USER, "refresh", NEXT)
    release(response({ token: "old-refresh", user: USER, session_mode: "refresh", expires_in: 900 }))
    await assertion
    expect(fetch.mock.calls.filter(([url]) => !url.includes("/auth/refresh"))).toHaveLength(1)
    expect(getAccessToken()).toBe("new-token")
  })
})
