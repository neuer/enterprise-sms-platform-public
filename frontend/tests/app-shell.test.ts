import { flushPromises, mount } from "@vue/test-utils"
import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { vi } from "vitest"

import App from "../src/App.vue"
import { getDashboard, type DashboardSnapshot } from "../src/api/dashboard"
import { useApprovalBadgeStore } from "../src/stores/approvalBadge"
import { SESSION_CLEAR_SIGNAL_KEY, useSessionStore } from "../src/stores/session"

vi.mock("../src/api/dashboard", () => ({ getDashboard: vi.fn() }))

const dashboardSnapshot = {
  refreshed_at: "2026-07-20T15:00:00+08:00",
  categories: [],
  overall_success_rate: 0,
  pending_approvals: 0,
  operations: {
    current_balance: 5000,
    balances: [],
    alerts: [],
    dispositions: { uncertain: 0, unmatched: 0, callback_dead: 0 },
    jobs: [],
    channel_monitor: {
      realtime_queue: 0,
      bulk_queue: 0,
      qps_used: 0,
      qps_rate: 5,
      reserved_realtime_qps: 2,
      stale: false,
    },
    balance_alert_threshold: 10000,
  },
  ui_policy: { test_send_max: 5 },
} satisfies DashboardSnapshot

describe("应用骨架", () => {
  beforeEach(() => {
    vi.mocked(getDashboard).mockReset()
    vi.mocked(getDashboard).mockResolvedValue(dashboardSnapshot)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("在非仪表盘页面使用后端快照显示顶栏厂商余额", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/apps", component: { template: "<div>应用管理</div>" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    await router.push("/apps")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    await flushPromises()

    expect(getDashboard).toHaveBeenCalledTimes(1)
    expect(wrapper.get(".balance").text()).toContain("5,000")
    expect(wrapper.get(".balance").attributes("aria-label")).toBe("厂商余额 5,000 计费条")
    wrapper.unmount()
  })

  it("仪表盘与顶栏共享同一余额快照且不重复轮询", async () => {
    vi.useFakeTimers()
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/dashboard", component: { template: "<div>仪表盘</div>" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    await router.push("/dashboard")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    await flushPromises()

    expect(getDashboard).not.toHaveBeenCalled()

    window.dispatchEvent(
      new CustomEvent("sms:dashboard-balance", {
        detail: { currentBalance: 4200 },
      }),
    )
    await flushPromises()
    expect(wrapper.get(".balance").text()).toContain("4,200")

    await vi.advanceTimersByTimeAsync(60_000)
    expect(getDashboard).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("呈现品牌和主导航且不伪造运行指标", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { template: "<div>仪表盘内容</div>" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    await router.push("/")
    await router.isReady()

    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })

    expect(wrapper.get("[data-testid='brand']").text()).toContain("青鸾")
    expect(wrapper.get("nav").text()).toContain("仪表盘")
    expect(wrapper.get("nav").text()).toContain("黑名单")
    expect(wrapper.get("nav").text()).toContain("敏感词")
    expect(wrapper.find("[data-testid='channel-strip']").exists()).toBe(false)
    expect(wrapper.text()).not.toContain("28,640")
    expect(wrapper.get("main").text()).toContain("仪表盘内容")
  })

  it("查看员只看到只读查询与报表导航", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { template: "<div />" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 2,
      identity_id: 12,
      provider_code: "local",
      username: "viewer01",
      display_name: "开发查看员",
      dept: "业务一部",
      role: "viewer",
    })
    await router.push("/")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    const navigation = wrapper.get("nav").text()
    expect(navigation).toContain("统计报表")
    expect(navigation).toContain("批次列表")
    expect(navigation).not.toContain("人工发送")
    expect(navigation).not.toContain("审批中心")
    expect(navigation).not.toContain("模板管理")
    expect(navigation).not.toContain("回调任务")
    expect(navigation).not.toContain("用户与角色")
  })

  it("审批员看到审批与只读模板签名但看不到发送和运维", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { template: "<div />" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 3,
      identity_id: 13,
      provider_code: "local",
      username: "approver01",
      display_name: "开发审批员",
      dept: "业务一部",
      role: "approver",
    })
    await router.push("/")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    const navigation = wrapper.get("nav").text()
    expect(navigation).toContain("审批中心")
    expect(navigation).toContain("模板管理")
    expect(navigation).toContain("签名管理")
    expect(navigation).not.toContain("人工发送")
    expect(navigation).not.toContain("运维中心")
  })

  it("审批员导航在审批中心项展示待审计数角标，零待审不渲染", async () => {
    const counts = { pending: 12, approved: 0, rejected: 0, expired: 0, pending_urgent: 3 }
    const fetch = vi.fn(async (url: string) => {
      expect(String(url)).toContain("/api/v1/web/approvals?")
      expect(String(url)).toContain("status=pending")
      expect(String(url)).toContain("size=1")
      return {
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => ({ total: 12, counts, items: [] }),
      }
    })
    vi.stubGlobal("fetch", fetch)
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { template: "<div />" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 3,
      identity_id: 13,
      provider_code: "local",
      username: "approver01",
      display_name: "开发审批员",
      dept: "业务一部",
      role: "approver",
    })
    await router.push("/")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    await flushPromises()

    const badge = wrapper.get("a[href='/approvals'] .nav-badge")
    expect(badge.text()).toBe("12")
    expect(badge.attributes("aria-label")).toContain("12 条待审批")

    useApprovalBadgeStore(pinia).pending = 0
    await flushPromises()
    expect(wrapper.find(".nav-badge").exists()).toBe(false)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("审批页路由期间暂停角标轮询，离开后立即补刷并恢复周期", async () => {
    vi.useFakeTimers()
    const counts = { pending: 5, approved: 0, rejected: 0, expired: 0, pending_urgent: 0 }
    const fetch = vi.fn(async (_url: string) => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ total: 5, counts, items: [] }),
    }))
    vi.stubGlobal("fetch", fetch)
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/approvals", component: { template: "<div>审批中心</div>" } },
        { path: "/batches", component: { template: "<div>批次列表</div>" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    await router.push("/approvals")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    await flushPromises()
    const badgeCalls = () => fetch.mock.calls.filter(([url]) => String(url).includes("/web/approvals?")).length

    // 审批页自身轮询会回写角标，全局角标轮询在此路由暂停，不重复请求
    expect(badgeCalls()).toBe(0)
    await vi.advanceTimersByTimeAsync(90_000)
    expect(badgeCalls()).toBe(0)

    await router.push("/batches")
    await flushPromises()
    expect(badgeCalls()).toBe(1)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(badgeCalls()).toBe(2)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("移动导航支持打开、路由后关闭和 Escape 关闭", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/dashboard", component: { template: "<div>仪表盘</div>" } },
        { path: "/reports", component: { template: "<div>统计报表</div>" } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    useSessionStore(pinia).apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    await router.push("/dashboard")
    await router.isReady()

    const wrapper = mount(App, { attachTo: document.body, global: { plugins: [pinia, router] } })
    const toggle = wrapper.get("[data-testid='navigation-toggle']")
    expect(wrapper.get("a[href='/dashboard']").attributes("aria-current")).toBe("page")

    await toggle.trigger("click")
    expect(wrapper.get(".app-shell").classes()).toContain("navigation-open")
    expect(toggle.attributes("aria-expanded")).toBe("true")

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
    await flushPromises()
    expect(wrapper.get(".app-shell").classes()).not.toContain("navigation-open")

    await toggle.trigger("click")
    await wrapper.get("a[href='/reports']").trigger("click")
    await flushPromises()
    expect(router.currentRoute.value.path).toBe("/reports")
    expect(wrapper.get(".app-shell").classes()).not.toContain("navigation-open")

    wrapper.unmount()
  })

  it("收到统一未授权事件时清除会话并返回登录页", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/dashboard", component: { template: "<div />" } },
        { path: "/login", component: { template: "<div>登录</div>" }, meta: { public: true } },
        { path: "/reports", component: { template: "<div />" } },
        { path: "/batches", component: { template: "<div />" } },
        { path: "/messages", component: { template: "<div />" } },
        { path: "/replies", component: { template: "<div />" } },
      ],
    })
    const pinia = createPinia()
    const session = useSessionStore(pinia)
    session.apply("jwt", {
      account_id: 2,
      identity_id: 12,
      provider_code: "local",
      username: "viewer01",
      display_name: "开发查看员",
      dept: "业务一部",
      role: "viewer",
    })
    await router.push("/dashboard")
    await router.isReady()
    mount(App, { global: { plugins: [pinia, router] } })

    window.dispatchEvent(new Event("sms:unauthorized"))
    await flushPromises()

    expect(session.isAuthenticated).toBe(false)
    expect(router.currentRoute.value.path).toBe("/login")
  })

  it("收到其他标签页的会话清除信号时立即清除当前会话", async () => {
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
    session.apply("jwt", {
      account_id: 2,
      identity_id: 12,
      provider_code: "local",
      username: "viewer01",
      display_name: "开发查看员",
      dept: "业务一部",
      role: "viewer",
    })
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })

    window.dispatchEvent(new StorageEvent("storage", { key: SESSION_CLEAR_SIGNAL_KEY }))
    await flushPromises()

    expect(session.isAuthenticated).toBe(false)
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(router.currentRoute.value.path).toBe("/login")
    wrapper.unmount()
  })

  it("跨标签页 Storage 信号会取消本页在途会话请求", async () => {
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
    session.apply("jwt", {
      account_id: 2,
      identity_id: 12,
      provider_code: "local",
      username: "viewer01",
      display_name: "开发查看员",
      dept: "业务一部",
      role: "viewer",
    })
    let aborted = false
    const fetch = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            aborted = true
            reject(new DOMException("会话已切换", "AbortError"))
          })
        }),
    )
    vi.stubGlobal("fetch", fetch)
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })
    const { authorizedFetch } = await import("../src/api/client")
    const pending = authorizedFetch("/api/v1/web/reports/dashboard", { method: "GET" })
    const assertion = expect(pending).rejects.toThrow("会话已切换")

    window.dispatchEvent(new StorageEvent("storage", { key: SESSION_CLEAR_SIGNAL_KEY }))
    await flushPromises()
    await assertion
    expect(aborted).toBe(true)
    expect(session.isAuthenticated).toBe(false)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("BFCache 恢复且服务端会话已注销时回到登录页", async () => {
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
    session.apply("jwt", {
      account_id: 2,
      identity_id: 12,
      provider_code: "local",
      username: "viewer01",
      display_name: "开发查看员",
      dept: "业务一部",
      role: "viewer",
    })
    vi.spyOn(session, "revalidateOnResume").mockResolvedValue(false)
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })

    const persisted = new Event("pageshow")
    Object.defineProperty(persisted, "persisted", { value: true })
    window.dispatchEvent(persisted)
    await flushPromises()

    expect(session.revalidateOnResume).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe("/login")
    wrapper.unmount()
  })

  it("本地账号可在顶栏日常改密且成功后强制重新登录", async () => {
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
    session.apply("jwt-local", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    const fetch = vi.fn(
      async (
        url: string,
      ): Promise<{
        ok: boolean
        status: number
        headers: { get: () => string | null }
        json: () => Promise<unknown>
      }> => {
        if (url.endsWith("/password-policy")) {
          return {
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({
              min_length: 12,
              max_length: 128,
              required_character_classes: 3,
              forbid_username: true,
              description: "12–128 位，至少包含三类字符，不能包含用户名",
            }),
          }
        }
        return { ok: true, status: 200, headers: { get: () => "0" }, json: async () => undefined }
      },
    )
    vi.stubGlobal("fetch", fetch)
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })

    expect(wrapper.find("[data-testid='change-password']").exists()).toBe(true)
    await wrapper.get("[data-testid='change-password']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='daily-current-password']").setValue("Current@Password123")
    await wrapper.get("[data-testid='daily-new-password']").setValue("Daily@Password456")
    await wrapper.get("[data-testid='daily-confirm-password']").setValue("Daily@Password456")
    await wrapper.get("[data-testid='daily-password-form']").trigger("submit")
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/auth/password/change",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer jwt-local" }),
        body: JSON.stringify({
          current_password: "Current@Password123",
          new_password: "Daily@Password456",
        }),
      }),
    )
    expect(session.isAuthenticated).toBe(false)
    expect(router.currentRoute.value.path).toBe("/login")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("公共登录路由不渲染业务侧栏", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/login", component: { template: "<div data-testid='login-page'>登录</div>" }, meta: { public: true } },
        { path: "/:pathMatch(.*)*", component: { template: "<div />" } },
      ],
    })
    await router.push("/login")
    await router.isReady()

    const wrapper = mount(App, { global: { plugins: [createPinia(), router] } })

    expect(wrapper.get("[data-testid='login-page']").text()).toBe("登录")
    expect(wrapper.find("nav").exists()).toBe(false)
  })

  it("退出后清除会话并回到登录页", async () => {
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
    session.apply("jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    await router.push("/dashboard")
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [pinia, router] } })

    await wrapper.get("[data-testid='logout']").trigger("click")
    await flushPromises()

    expect(session.isAuthenticated).toBe(false)
    expect(router.currentRoute.value.path).toBe("/login")
    vi.unstubAllGlobals()
  })
})
