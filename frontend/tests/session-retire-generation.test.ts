import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { SessionDocument } from "../src/api/sessionDocument"
import { redirectToLoginIfCleared, runAppLogout } from "../src/api/sessionNavigation"
import { SESSION_CLEAR_SIGNAL_KEY, type SessionRetiredMessage } from "../src/api/sessionSignals"
import { createSessionStore } from "../src/stores/session"
import { installTestWebLocks } from "./setup-session"

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

type ContextLabel = "A" | "B"

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

interface QueuedSignal {
  from: ContextLabel
  message: SessionRetiredMessage
}

class ProgrammableSessionBus {
  readonly queue: QueuedSignal[] = []
  readonly published: QueuedSignal[] = []

  publish(from: ContextLabel, message: SessionRetiredMessage): void {
    const item = { from, message }
    this.queue.push(item)
    this.published.push(item)
  }

  take(): QueuedSignal {
    const item = this.queue.shift()
    if (!item) throw new Error("event queue empty")
    return item
  }

  remaining(): number {
    return this.queue.length
  }

  takeFrom(from: ContextLabel): QueuedSignal {
    const index = this.queue.findIndex((item) => item.from === from)
    if (index < 0) throw new Error(`no queued signal from ${from}`)
    const [item] = this.queue.splice(index, 1)
    return item
  }
}

interface IsolatedSessionContext {
  label: ContextLabel
  store: ReturnType<ReturnType<typeof createSessionStore>>
  document: SessionDocument
}

function importIsolatedContext(label: ContextLabel, bus: ProgrammableSessionBus): IsolatedSessionContext {
  const document = new SessionDocument({
    publisher: (message) => bus.publish(label, message),
  })
  const pinia = createPinia()
  const store = createSessionStore(document, `session-${label}`)(pinia)
  return { label, store, document }
}

function createDualContexts(): {
  bus: ProgrammableSessionBus
  ctxA: IsolatedSessionContext
  ctxB: IsolatedSessionContext
} {
  const bus = new ProgrammableSessionBus()
  const ctxB = importIsolatedContext("B", bus)
  const ctxA = importIsolatedContext("A", bus)
  return { bus, ctxA, ctxB }
}

function memoryRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/dashboard", component: { template: "<div />" } },
      { path: "/login", component: { template: "<div>登录</div>" }, meta: { public: true } },
    ],
  })
}

async function readyRouter(path = "/dashboard") {
  const router = memoryRouter()
  await router.push(path)
  await router.isReady()
  return router
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function bearerToken(init?: RequestInit): string {
  const headers = init?.headers
  if (!headers || typeof headers !== "object") return ""
  const value = (headers as Record<string, string>).Authorization ?? ""
  return value.startsWith("Bearer ") ? value.slice("Bearer ".length) : ""
}

type FixtureUser = typeof admin | typeof operator

function createProgrammableAuth(
  options: {
    sessionMode?: "access_only" | "refresh"
    loginUser?: FixtureUser
    loginToken?: string | ((call: number) => string)
    loginUserForCall?: (call: number) => FixtureUser
    holdLogin?: boolean
  } = {},
) {
  const logoutGates = new Map<string, Deferred<Response>>()
  const loginGates: Array<Deferred<Response>> = []
  const refreshGates: Array<Deferred<Response>> = []
  let loginCalls = 0
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    const path = String(url)
    if (path.includes("/auth/logout")) {
      const token = bearerToken(init)
      const gate = deferred<Response>()
      logoutGates.set(token, gate)
      init?.signal?.addEventListener(
        "abort",
        () => {
          gate.reject(new DOMException("会话已切换", "AbortError"))
        },
        { once: true },
      )
      return gate.promise
    }
    if (path.includes("/auth/login")) {
      loginCalls += 1
      const token =
        typeof options.loginToken === "function"
          ? options.loginToken(loginCalls)
          : (options.loginToken ?? `access-${loginCalls}`)
      const user = options.loginUserForCall?.(loginCalls) ?? options.loginUser ?? operator
      const body = {
        session_mode: options.sessionMode ?? "access_only",
        token,
        expires_in: 900,
        ...(options.sessionMode === "refresh" ? { refresh_expires_in: 604800 } : {}),
        user,
      }
      if (options.holdLogin) {
        const gate = deferred<Response>()
        loginGates.push(gate)
        return gate.promise.then(() => jsonResponse(body))
      }
      return Promise.resolve(jsonResponse(body))
    }
    if (path.includes("/auth/refresh")) {
      const gate = deferred<Response>()
      refreshGates.push(gate)
      return gate.promise
    }
    return Promise.resolve(jsonResponse({}, 204))
  })
  return { fetch, logoutGates, loginGates, refreshGates, loginCalls: () => loginCalls }
}

function expectNoCredentials(message: SessionRetiredMessage): void {
  expect(Object.keys(message)).toEqual(["version", "type", "target_instance_id", "event_id"])
  const encoded = JSON.stringify(message)
  expect(encoded).not.toMatch(/eyJ[A-Za-z0-9_-]+/)
  expect(encoded).not.toContain("Bearer")
  expect(encoded).not.toContain("password")
  expect(encoded).not.toContain("sms_refresh_token")
  expect(encoded).not.toMatch(/"jti"/)
  expect(encoded).not.toMatch(/"token"/)
  expect(message.target_instance_id).toMatch(/^[0-9a-f]{32}$/)
  expect(message.event_id).toMatch(/^[0-9a-f]{32}$/)
}

async function deliver(
  ctx: IsolatedSessionContext,
  signal: QueuedSignal,
  router?: Awaited<ReturnType<typeof readyRouter>>,
): Promise<boolean> {
  const changed = ctx.store.applyRemoteSessionRetired(signal.message)
  if (router) await redirectToLoginIfCleared(changed, router)
  return changed
}

async function flushQueue(
  bus: ProgrammableSessionBus,
  ctxA: IsolatedSessionContext,
  ctxB: IsolatedSessionContext,
  routerA?: Awaited<ReturnType<typeof readyRouter>>,
  routerB?: Awaited<ReturnType<typeof readyRouter>>,
): Promise<void> {
  while (bus.remaining() > 0) {
    const signal = bus.take()
    const target = signal.from === "A" ? ctxB : ctxA
    const router = signal.from === "A" ? routerB : routerA
    await deliver(target, signal, router)
  }
}

describe("AUTH-R5-04 定向会话退役", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.unstubAllGlobals()
    vi.stubGlobal("navigator", {})
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    installTestWebLocks()
  })

  it("test_stale_logout_finally_does_not_clear_new_session", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    expect(ctxA.document.generation).toBe(0)
    expect(ctxB.document.generation).toBe(0)
    ctxA.document.invalidateGeneration()
    expect(ctxA.document.generation).not.toBe(ctxB.document.generation)

    const auth = createProgrammableAuth({ loginToken: "a-new-access", loginUser: operator })
    vi.stubGlobal("fetch", auth.fetch)
    ctxB.store.apply("b-old-access", admin, "access_only")
    const oldInstance = ctxB.store.sessionInstanceId
    const routerA = await readyRouter("/dashboard")
    const routerB = await readyRouter("/dashboard")

    const logoutP = runAppLogout({
      logout: () => ctxB.store.logout(),
      isAuthenticated: () => ctxB.store.isAuthenticated,
      redirectToLogin: () => redirectToLoginIfCleared(true, routerB),
    })
    await vi.waitFor(() => expect(auth.logoutGates.has("b-old-access")).toBe(true))

    await expect(ctxA.store.login("local", "operator01", "Temp@Password123")).resolves.toEqual({
      nextAction: "authenticated",
    })
    expect(ctxA.store.token).toBe("a-new-access")
    expect(ctxA.store.sessionInstanceId).not.toBe(oldInstance)

    const retireOld = bus.takeFrom("A")
    expect(retireOld.message.target_instance_id).toBe(oldInstance)
    expect(await deliver(ctxB, retireOld, routerB)).toBe(true)
    expect(ctxB.store.isAuthenticated).toBe(false)
    expect(ctxB.document.getAccessToken()).toBeNull()

    await logoutP
    await flushQueue(bus, ctxA, ctxB, routerA, routerB)

    expect(ctxA.store.token).toBe("a-new-access")
    expect(ctxA.store.username).toBe("operator01")
    expect(ctxA.store.sessionMode).toBe("access_only")
    expect(ctxA.document.getAccessToken()).toBe("a-new-access")
    expect(routerA.currentRoute.value.path).toBe("/dashboard")
    expect(ctxB.store.isAuthenticated).toBe(false)
    expect(bus.published.filter((item) => item.from === "B")).toHaveLength(0)
    expect(bus.published.every((item) => item.message.type === "session-retired")).toBe(true)
  })

  it("test_remote_clear_aborts_old_logout_without_rebroadcast", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    const auth = createProgrammableAuth({ loginToken: "a-new-access" })
    vi.stubGlobal("fetch", auth.fetch)
    ctxB.store.apply("b-old-access", admin, "access_only")
    const oldInstance = ctxB.store.sessionInstanceId

    const logoutP = ctxB.store.logout()
    await vi.waitFor(() => expect(auth.logoutGates.has("b-old-access")).toBe(true))
    await ctxA.store.login("local", "operator01", "Temp@Password123")
    const retireOld = bus.takeFrom("A")
    expect(retireOld.message.target_instance_id).toBe(oldInstance)
    expect(ctxB.store.applyRemoteSessionRetired(retireOld.message)).toBe(true)
    await expect(logoutP).resolves.toEqual({ cleared: false })
    expect(bus.published.filter((item) => item.from === "B")).toHaveLength(0)
    expect(ctxA.store.token).toBe("a-new-access")
  })

  it("test_logout_network_failure_clears_only_original_instance", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    const auth = createProgrammableAuth({ loginToken: "a-new-access" })
    vi.stubGlobal("fetch", auth.fetch)
    ctxA.store.apply("a-live-access", operator, "access_only")
    ctxB.store.apply("b-old-access", admin, "access_only")
    const aInstance = ctxA.store.sessionInstanceId
    const bInstance = ctxB.store.sessionInstanceId
    expect(aInstance).not.toBe(bInstance)

    const logoutP = ctxB.store.logout()
    await vi.waitFor(() => expect(auth.logoutGates.has("b-old-access")).toBe(true))
    auth.logoutGates.get("b-old-access")?.reject(new Error("offline"))
    await expect(logoutP).rejects.toThrow("offline")

    expect(ctxB.store.isAuthenticated).toBe(false)
    expect(ctxB.document.getAccessToken()).toBeNull()
    expect(ctxA.store.token).toBe("a-live-access")
    expect(ctxA.store.sessionInstanceId).toBe(aInstance)
    const fromB = bus.published.filter((item) => item.from === "B")
    expect(fromB).toHaveLength(1)
    expect(fromB[0]?.message.target_instance_id).toBe(bInstance)
    expect(ctxA.store.applyRemoteSessionRetired(fromB[0]!.message)).toBe(false)
  })

  it("test_queued_old_logout_cannot_run_under_new_generation", async () => {
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    const auth = createProgrammableAuth({
      sessionMode: "access_only",
      holdLogin: true,
      loginToken: "queued-new-access",
      loginUser: operator,
    })
    vi.stubGlobal("fetch", auth.fetch)
    ctx.store.apply("queued-old-access", admin, "access_only")
    const oldInstance = ctx.store.sessionInstanceId
    const oldGeneration = ctx.document.generation

    const loginP = ctx.store.login("local", "operator01", "Temp@Password123")
    await vi.waitFor(() => expect(auth.loginGates.length).toBe(1))
    const logoutP = ctx.store.logout()
    auth.loginGates[0]?.resolve(jsonResponse({}))
    await expect(loginP).resolves.toEqual({ nextAction: "authenticated" })
    await expect(logoutP).resolves.toEqual({ cleared: false })

    expect(ctx.store.token).toBe("queued-new-access")
    expect(ctx.store.sessionInstanceId).not.toBe(oldInstance)
    expect(ctx.document.generation).toBeGreaterThan(oldGeneration)
    expect(ctx.document.getAccessToken()).toBe("queued-new-access")
    expect(bus.published.every((item) => item.message.target_instance_id === oldInstance)).toBe(true)
  })

  it("test_same_account_relogin_has_distinct_session_instance", async () => {
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    const auth = createProgrammableAuth({
      loginUser: admin,
      loginToken: (call) => `same-account-${call}`,
    })
    vi.stubGlobal("fetch", auth.fetch)

    await ctx.store.login("local", "admin", "Temp@Password123")
    const first = ctx.store.sessionInstanceId
    await ctx.store.login("local", "admin", "Temp@Password123")
    const second = ctx.store.sessionInstanceId

    expect(first).toMatch(/^[0-9a-f]{32}$/)
    expect(second).toMatch(/^[0-9a-f]{32}$/)
    expect(second).not.toBe(first)
    expect(second).not.toBe(String(admin.account_id))
    expect(second).not.toBe(admin.username)
    expect(ctx.store.token).toBe("same-account-2")
    expect(ctx.store.username).toBe("admin")
  })

  it("test_delayed_duplicate_clear_ignores_new_instance", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    const auth = createProgrammableAuth({ loginToken: "a-new-access" })
    vi.stubGlobal("fetch", auth.fetch)
    ctxB.store.apply("b-old-access", admin, "access_only")
    const oldInstance = ctxB.store.sessionInstanceId
    await ctxA.store.login("local", "operator01", "Temp@Password123")
    const newInstance = ctxA.store.sessionInstanceId
    const retireOld = bus.takeFrom("A")
    expect(retireOld.message.target_instance_id).toBe(oldInstance)
    expect(ctxB.store.applyRemoteSessionRetired(retireOld.message)).toBe(true)
    expect(ctxA.store.applyRemoteSessionRetired(retireOld.message)).toBe(false)
    expect(ctxA.store.applyRemoteSessionRetired(retireOld.message)).toBe(false)
    expect(ctxA.store.sessionInstanceId).toBe(newInstance)
    expect(ctxA.store.token).toBe("a-new-access")
  })

  it("test_storage_remove_event_is_not_a_clear_command", async () => {
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    ctx.store.apply("live-access", operator, "access_only")
    expect(
      ctx.store.applyStorageSessionSignal({
        key: SESSION_CLEAR_SIGNAL_KEY,
        newValue: null,
      }),
    ).toBe(false)
    expect(ctx.store.isAuthenticated).toBe(true)
    expect(ctx.document.getAccessToken()).toBe("live-access")
  })

  it("test_malformed_or_unknown_signal_is_ignored", async () => {
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    ctx.store.apply("live-access", operator, "access_only")
    const ignored = [
      "1700000000000",
      "{not-json",
      JSON.stringify({
        version: 2,
        type: "session-retired",
        target_instance_id: "a".repeat(32),
        event_id: "b".repeat(32),
      }),
      JSON.stringify({
        version: 1,
        type: "session-cleared",
        target_instance_id: "a".repeat(32),
        event_id: "b".repeat(32),
      }),
      JSON.stringify({ version: 1, type: "session-retired", event_id: "b".repeat(32) }),
      JSON.stringify({
        version: 1,
        type: "session-retired",
        target_instance_id: "a".repeat(32),
        event_id: "b".repeat(32),
        token: "jwt-should-reject",
      }),
    ]
    for (const raw of ignored) {
      expect(ctx.store.applyRemoteSessionRetired(raw)).toBe(false)
      expect(ctx.store.applyStorageSessionSignal({ key: SESSION_CLEAR_SIGNAL_KEY, newValue: raw })).toBe(false)
    }
    expect(ctx.store.isAuthenticated).toBe(true)
    expect(ctx.store.token).toBe("live-access")
  })

  it("test_storage_unavailable_preserves_local_cleanup", async () => {
    const nativeSetItem = Storage.prototype.setItem
    const writes: Array<[string, string]> = []
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      writes.push([key, value])
      if (key === SESSION_CLEAR_SIGNAL_KEY || key === "sms_session_instance") {
        throw new DOMException("restricted", "SecurityError")
      }
      return nativeSetItem.call(this, key, value)
    })
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    ctx.store.apply("doomed-access", admin, "access_only")
    ctx.document.installPublisher(null)
    const auth = createProgrammableAuth()
    vi.stubGlobal("fetch", auth.fetch)
    const logoutP = ctx.store.logout()
    await vi.waitFor(() => expect(auth.logoutGates.has("doomed-access")).toBe(true))
    auth.logoutGates.get("doomed-access")?.resolve(jsonResponse({}, 204))
    await expect(logoutP).resolves.toEqual({ cleared: true })
    expect(ctx.store.isAuthenticated).toBe(false)
    expect(ctx.document.getAccessToken()).toBeNull()
    expect(writes.some(([, value]) => /^\d+$/.test(value))).toBe(false)
    expect(bus.published).toHaveLength(0)
    setItemSpy.mockRestore()
  })

  it("test_stale_logout_cannot_redirect_new_session_to_login", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    const auth = createProgrammableAuth({ loginToken: "a-new-access" })
    vi.stubGlobal("fetch", auth.fetch)
    ctxB.store.apply("b-old-access", admin, "access_only")
    const routerA = await readyRouter("/dashboard")
    const routerB = await readyRouter("/dashboard")

    const logoutP = runAppLogout({
      logout: () => ctxB.store.logout(),
      isAuthenticated: () => ctxB.store.isAuthenticated,
      redirectToLogin: () => redirectToLoginIfCleared(true, routerB),
      onUnconfirmedRevoke: () => {
        throw new Error("stale logout must not toast against the new session")
      },
    })
    await vi.waitFor(() => expect(auth.logoutGates.has("b-old-access")).toBe(true))
    await ctxA.store.login("local", "operator01", "Temp@Password123")
    const retireOld = bus.takeFrom("A")
    await deliver(ctxB, retireOld, routerB)
    await logoutP
    await flushQueue(bus, ctxA, ctxB, routerA, routerB)

    expect(ctxA.store.isAuthenticated).toBe(true)
    expect(routerA.currentRoute.value.path).toBe("/dashboard")
    expect(routerB.currentRoute.value.path).toBe("/login")
  })

  it("test_refresh_preserves_logical_instance_and_existing_security", async () => {
    installTestWebLocks()
    const bus = new ProgrammableSessionBus()
    const ctx = importIsolatedContext("A", bus)
    const auth = createProgrammableAuth({
      sessionMode: "refresh",
      loginUser: admin,
      loginToken: "refresh-access-1",
    })
    vi.stubGlobal("fetch", auth.fetch)
    await ctx.store.login("local", "admin", "Temp@Password123")
    const instance = ctx.store.sessionInstanceId
    expect(ctx.store.sessionMode).toBe("refresh")
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(localStorage.getItem("sms_token")).toBeNull()

    const resume = ctx.store.revalidateOnResume()
    await vi.waitFor(() => expect(auth.refreshGates.length).toBe(1))
    auth.refreshGates[0]?.resolve(
      jsonResponse({
        session_mode: "refresh",
        token: "refresh-access-2",
        expires_in: 900,
        refresh_expires_in: 604800,
        user: admin,
      }),
    )
    await expect(resume).resolves.toBe(true)
    expect(ctx.store.sessionInstanceId).toBe(instance)
    expect(ctx.store.token).toBe("refresh-access-2")
    expect(ctx.document.getAccessToken()).toBe("refresh-access-2")
    expect(sessionStorage.getItem("sms_token")).toBeNull()

    vi.stubGlobal("navigator", {})
    const accessOnly = importIsolatedContext("B", bus)
    const blocked = createProgrammableAuth({ sessionMode: "access_only" })
    vi.stubGlobal("fetch", blocked.fetch)
    await expect(accessOnly.store.restoreFromCookie()).resolves.toBe(false)
    expect(blocked.fetch.mock.calls.some(([url]) => String(url).includes("/auth/refresh"))).toBe(false)
    accessOnly.store.apply("ao-access", admin, "access_only")
    expect(accessOnly.store.sessionMode).toBe("access_only")
    expect(accessOnly.store.token).toBe("ao-access")
  })

  it("test_session_messages_never_include_credentials", async () => {
    const { bus, ctxA, ctxB } = createDualContexts()
    const auth = createProgrammableAuth({ loginToken: "a-new-access" })
    vi.stubGlobal("fetch", auth.fetch)
    ctxB.store.apply("b-old-access", admin, "access_only")
    await ctxA.store.login("local", "operator01", "Secret@Password123")
    ctxB.store.clearAllTabs()
    expect(bus.published.length).toBeGreaterThan(0)
    for (const item of bus.published) {
      expectNoCredentials(item.message)
      expect(JSON.stringify(item.message)).not.toContain("Secret@Password123")
      expect(JSON.stringify(item.message)).not.toContain("a-new-access")
      expect(JSON.stringify(item.message)).not.toContain("b-old-access")
    }
  })
})
