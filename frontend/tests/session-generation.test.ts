import { flushPromises, mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, beforeEach, vi } from "vitest"

import App from "../src/App.vue"
import { apiRequest } from "../src/api/webMessages"
import {
  getSessionGeneration,
  invalidateSessionGeneration,
  isCurrentSessionGeneration,
} from "../src/api/sessionGeneration"
import {
  clearRefreshTabBinding,
  getAccessToken,
  getSessionUser,
  REFRESH_TAB_ID_KEY,
  resetAccessSessionModule,
  setAccessSession,
} from "../src/api/sessionTokens"
import { SESSION_CLEAR_SIGNAL_KEY, useSessionStore } from "../src/stores/session"

// TEST-MANUAL #442 / 8.1.1-4：真实双标签页中，B 有在途 Refresh 时 A 登录新账号，
// B 不得恢复旧主体，且不得把 Access/Refresh 写入 Web Storage。

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
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

const tabId = "b".repeat(32)

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

function holdRefresh(): {
  fetch: ReturnType<typeof vi.fn>
  releaseRefresh: (body: unknown, status?: number) => void
  refreshSignals: AbortSignal[]
} {
  const refreshSignals: AbortSignal[] = []
  const pending: Array<(value: Response) => void> = []
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/v1/web/auth/refresh") {
      if (init?.signal) refreshSignals.push(init.signal)
      return new Promise<Response>((resolve) => {
        pending.push(resolve)
      })
    }
    if (url === "/api/v1/web/auth/login") {
      return Promise.resolve(
        jsonResponse({
          token: "new-access",
          expires_in: 900,
          refresh_expires_in: 604800,
          user: operator,
        }),
      )
    }
    if (url === "/api/v1/web/auth/logout") {
      return Promise.resolve(jsonResponse({}, 204))
    }
    return Promise.resolve(jsonResponse({ code: "UNAUTHORIZED" }, 401))
  })
  return {
    fetch,
    releaseRefresh(body: unknown, status = 200) {
      const resolve = pending.shift()
      if (!resolve) throw new Error("no held refresh")
      resolve(jsonResponse(body, status))
    },
    refreshSignals,
  }
}

function staleRefreshBody(token = "stale-access") {
  return {
    token,
    expires_in: 900,
    refresh_expires_in: 604800,
    user: admin,
  }
}

async function waitForHeldRefresh(held: { refreshSignals: AbortSignal[] }, count = 1): Promise<void> {
  await vi.waitFor(() => {
    expect(held.refreshSignals.length).toBeGreaterThanOrEqual(count)
  })
}

describe("会话代际与跨标签页 Refresh 写回", () => {
  let setItemTrap: ReturnType<typeof trapBearerStorageWrites> | undefined

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    clearRefreshTabBinding()
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, tabId)
    vi.unstubAllGlobals()
    setActivePinia(createPinia())
    setItemTrap = trapBearerStorageWrites()
  })

  afterEach(() => {
    setItemTrap?.mockRestore()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it("401 单飞 Refresh 在 Storage 清理后不得写回旧 Access Token", async () => {
    const session = useSessionStore()
    setAccessSession("expired", admin)
    session.apply("expired", admin)
    let releaseRefresh!: (value: Response) => void
    const signals: AbortSignal[] = []
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/web/auth/refresh") {
        if (init?.signal) signals.push(init.signal)
        return new Promise<Response>((resolve) => {
          releaseRefresh = resolve
        })
      }
      return Promise.resolve(jsonResponse({ code: "UNAUTHORIZED" }, 401))
    })
    vi.stubGlobal("fetch", fetch)
    const pending = apiRequest("/reports/dashboard", { method: "GET" })
    await vi.waitFor(() => {
      expect(typeof releaseRefresh).toBe("function")
    })
    const epochBeforeClear = getSessionGeneration()

    window.dispatchEvent(new Event("sms:session-clearing"))
    session.clear()
    expect(signals[0]?.aborted).toBe(true)
    expect(isCurrentSessionGeneration(epochBeforeClear)).toBe(false)

    releaseRefresh(jsonResponse(staleRefreshBody()))
    await expect(pending).rejects.toThrow()
    expect(getAccessToken()).toBeNull()
    expect(getSessionUser()).toBeNull()
    expect(session.isAuthenticated).toBe(false)
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(localStorage.getItem("sms_token")).toBeNull()
  })

  it("兄弟标签页登录后的 StorageEvent 不得让本页恢复旧主体", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/dashboard", component: { template: "<div />" } },
        { path: "/login", component: { template: "<div>登录</div>" }, meta: { public: true } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    const session = useSessionStore(pinia)
    session.apply("expired", admin)
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    const pending = session.revalidateOnResume()
    await waitForHeldRefresh(held)

    window.dispatchEvent(new StorageEvent("storage", { key: SESSION_CLEAR_SIGNAL_KEY, newValue: "1700000000000" }))
    await flushPromises()
    held.releaseRefresh(staleRefreshBody("old-subject-access"))
    await expect(pending).resolves.toBe(false)

    expect(session.isAuthenticated).toBe(false)
    expect(session.username).toBe("")
    expect(getAccessToken()).toBeNull()
    expect(getSessionUser()).toBeNull()
    expect(router.currentRoute.value.path).toBe("/login")
    expect(localStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    wrapper.unmount()
  })

  it("新登录永久作废旧代 Refresh，后到的旧 Access Token 不得覆盖新主体", async () => {
    const session = useSessionStore()
    session.apply("expired", admin)
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)
    const pendingRefresh = session.revalidateOnResume()
    await waitForHeldRefresh(held)

    await expect(session.login("local", "operator01", "Temp@Password123")).resolves.toEqual({
      nextAction: "authenticated",
    })
    expect(session.username).toBe("operator01")
    expect(getAccessToken()).toBe("new-access")

    held.releaseRefresh(staleRefreshBody("old-access-after-login"))
    await expect(pendingRefresh).resolves.toBe(false)
    expect(getAccessToken()).toBe("new-access")
    expect(getSessionUser()).toEqual(operator)
    expect(session.accountId).toBe(9)
    expect(localStorage.getItem("sms_session_clear")).toBeNull()
  })

  it("restore 与 BFCache revalidate 共用同一代际，乱序旧 Refresh 不得获胜", async () => {
    const session = useSessionStore()
    session.apply("expired", admin)
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)
    const firstResume = session.revalidateOnResume()
    await waitForHeldRefresh(held, 1)
    const generationAtRefresh = getSessionGeneration()

    const secondResume = session.revalidateOnResume()
    await waitForHeldRefresh(held, 2)
    expect(isCurrentSessionGeneration(generationAtRefresh)).toBe(false)

    held.releaseRefresh(staleRefreshBody("pre-resume-access"))
    held.releaseRefresh({
      token: "resume-access",
      expires_in: 900,
      refresh_expires_in: 604800,
      user: admin,
    })
    await expect(firstResume).resolves.toBe(false)
    await expect(secondResume).resolves.toBe(true)
    expect(getAccessToken()).toBe("resume-access")
    expect(getAccessToken()).not.toBe("pre-resume-access")
  })

  it("Cookie 恢复过程中代际失效则丢弃后到的 Refresh 写回", async () => {
    const session = useSessionStore()
    session.resetIdentity()
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)
    const pending = session.restoreFromCookie()
    await waitForHeldRefresh(held)

    invalidateSessionGeneration()
    held.releaseRefresh(staleRefreshBody("restore-stale"))
    await expect(pending).resolves.toBe(false)
    expect(getAccessToken()).toBeNull()
    expect(session.isAuthenticated).toBe(false)
  })

  it("延迟返回的旧 Refresh 在跨页清理后仍不得写回", async () => {
    const session = useSessionStore()
    session.apply("expired", admin)
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)
    const pending = session.revalidateOnResume()
    await waitForHeldRefresh(held)

    await new Promise((resolve) => window.setTimeout(resolve, 25))
    session.clear()
    await new Promise((resolve) => window.setTimeout(resolve, 25))
    held.releaseRefresh(staleRefreshBody("delayed-stale"))
    await expect(pending).resolves.toBe(false)
    expect(getAccessToken()).toBeNull()
    expect(session.isAuthenticated).toBe(false)
  })

  it("login/refresh/logout/restore/BFCache 共用 sms-refresh-rotation 锁", async () => {
    const names: string[] = []
    const locks = {
      request: async (name: string, callback: () => Promise<unknown>) => {
        names.push(name)
        return callback()
      },
    }
    vi.stubGlobal("navigator", { locks })
    const session = useSessionStore()
    session.apply("expired", admin)
    const held = holdRefresh()
    vi.stubGlobal("fetch", held.fetch)

    const resume = session.revalidateOnResume()
    await waitForHeldRefresh(held)
    expect(names).toContain("sms-refresh-rotation")
    held.releaseRefresh({
      token: "rotated-once",
      expires_in: 900,
      refresh_expires_in: 604800,
      user: admin,
    })
    await expect(resume).resolves.toBe(true)

    session.resetIdentity()
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, tabId)
    const restore = session.restoreFromCookie()
    await waitForHeldRefresh(held, 2)
    held.releaseRefresh({
      token: "restored",
      expires_in: 900,
      refresh_expires_in: 604800,
      user: admin,
    })
    await expect(restore).resolves.toBe(true)

    const resumeAgain = session.revalidateOnResume()
    await waitForHeldRefresh(held, 3)
    held.releaseRefresh({
      token: "resumed",
      expires_in: 900,
      refresh_expires_in: 604800,
      user: admin,
    })
    await expect(resumeAgain).resolves.toBe(true)

    await session.login("local", "operator01", "Temp@Password123")
    await session.logout()
    expect(names.every((name) => name === "sms-refresh-rotation")).toBe(true)
    expect(names.length).toBeGreaterThanOrEqual(4)
  })
})
