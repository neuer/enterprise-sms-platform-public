import { flushPromises, mount } from "@vue/test-utils"
import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { vi } from "vitest"

import App from "../src/App.vue"
import { SESSION_CLEAR_SIGNAL_KEY, useSessionStore } from "../src/stores/session"

describe("应用骨架", () => {
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
    useSessionStore(pinia).apply("jwt", "refresh.jwt", {
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
    useSessionStore(pinia).apply("jwt", "refresh.jwt", {
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
    useSessionStore(pinia).apply("jwt", "refresh.jwt", {
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
    useSessionStore(pinia).apply("jwt", "refresh.jwt", {
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
    session.apply("jwt", "refresh.jwt", {
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
    session.apply("jwt", "refresh.jwt", {
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
    session.apply("jwt-local", "refresh.jwt", {
      account_id: 1,
      identity_id: 11,
      provider_code: "local",
      username: "admin01",
      display_name: "开发管理员",
      dept: "平台技术部",
      role: "admin",
    })
    const fetch = vi.fn(async (url: string): Promise<{
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
    })
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
    session.apply("jwt", "refresh.jwt", {
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
