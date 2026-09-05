import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import UserView from "../src/views/UserView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name === "content-length" && body === undefined ? "0" : null) },
    json: async () => body,
  }
}

const localUser = {
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

const adUser = {
  account_id: 21,
  identity_id: 31,
  provider_code: "ad",
  username: "ad.operator",
  display_name: "目录操作员",
  dept: "业务二部",
  role: "operator",
  role_override: false,
  status: 1,
  identity_status: 1,
  credential_status: null,
  source_groups: ["CN=SMS-Operators,OU=Groups,DC=example,DC=com"],
  sync_status: "synced",
  last_synced_at: "2026-07-16T08:00:00+08:00",
  last_login_at: "2026-07-16T08:00:00+08:00",
}

const policy = {
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名",
}

function listBody(items = [localUser, adUser]) {
  return { items, total: items.length, page: 1, page_size: 20 }
}

function routeFetch(overrides?: (url: string, init: RequestInit) => unknown) {
  return vi.fn().mockImplementation((input: string, init: RequestInit = {}) => {
    if (overrides) {
      const overridden = overrides(input, init)
      if (overridden) return Promise.resolve(overridden)
    }
    if (input === "/api/v1/web/auth/password-policy") return Promise.resolve(response(policy))
    if (input.startsWith("/api/v1/web/admin/users?")) return Promise.resolve(response(listBody()))
    return Promise.resolve(response(undefined))
  })
}

describe("用户与角色", () => {
  beforeEach(() => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  })

  afterEach(() => {
    sessionStorage.clear()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("同时呈现本地与 AD 身份状态、规则和完整移动操作", async () => {
    vi.stubGlobal("fetch", routeFetch())

    const wrapper = mount(UserView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.getComponent({ name: "ElTable" }).props("rowKey")).toBe("account_id")
    expect(wrapper.text()).toContain("本地账号")
    expect(wrapper.text()).toContain("AD 账号")
    expect(wrapper.text()).toContain("首次登录待改密")
    expect(wrapper.text()).toContain("已同步")
    expect(wrapper.text()).toContain("跟随 AD")
    expect(wrapper.text()).toContain("3–64 位 ASCII 字母")
    expect(wrapper.text()).toContain("12–128 位")
    expect(wrapper.text()).toContain("不能包含用户名")
    expect(wrapper.find("[data-testid='reset-password-8']").exists()).toBe(true)
    expect(wrapper.find("[data-testid='reset-password-21']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='mobile-reset-password-8']").exists()).toBe(true)
    expect(wrapper.find("[data-testid='mobile-status-21']").exists()).toBe(true)
    expect(wrapper.text()).toContain("最近登录 2026-07-16 08:00:00")

    await wrapper.get("[data-testid='role-21']").trigger("click")
    expect(wrapper.find("[data-testid='override-switch']").exists()).toBe(true)
    expect(wrapper.text()).toContain("按最近来源组和当前映射恢复角色")
    expect(wrapper.get("[data-testid='role-permission']").text()).toContain("Web 人工发送")
    wrapper.unmount()
  })

  it("通过右侧抽屉创建本地账号且临时密码只进入请求", async () => {
    const fetch = routeFetch((url) => {
      if (url === "/api/v1/web/admin/users/local") return response(localUser)
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='create-local-user']").trigger("click")
    expect(wrapper.get("[data-testid='create-role-permission']").text()).toContain("本部门记录与报表")
    await wrapper.get("[data-testid='create-username']").setValue("new.user")
    await wrapper.get("[data-testid='create-display-name']").setValue("新用户")
    await wrapper.get("[data-testid='create-password']").setValue("Temporary@123")
    await wrapper.get("[data-testid='save-local-user']").trigger("click")
    await flushPromises()

    const request = fetch.mock.calls.find(([url]) => url === "/api/v1/web/admin/users/local")
    expect(request).toBeTruthy()
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      username: "new.user",
      display_name: "新用户",
      dept: "",
      role: "viewer",
      temporary_password: "Temporary@123",
    })
    expect(JSON.stringify(localUser)).not.toContain("Temporary@123")
    wrapper.unmount()
  })

  it("本地重置、账号停用与强制下线都使用 account_id 并显式确认", async () => {
    const fetch = routeFetch()
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='reset-password-8']").trigger("click")
    await wrapper.get("[data-testid='reset-password-input']").setValue("Reset@Password123")
    await wrapper.get("[data-testid='confirm-password-reset']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='status-21']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='revoke-21']").trigger("click")
    await flushPromises()

    const paths = fetch.mock.calls.map(([url]) => url)
    expect(paths).toContain("/api/v1/web/admin/users/8/password/reset")
    expect(paths).toContain("/api/v1/web/admin/users/21/status")
    expect(paths).toContain("/api/v1/web/admin/users/21/sessions/revoke")
    expect(ElMessageBox.confirm).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it("明确展示最后管理员保护错误且保留当前列表", async () => {
    const error = vi.spyOn(ElMessage, "error")
    const fetch = routeFetch((url) => {
      if (url === "/api/v1/web/admin/users/21/status") {
        return response({ code: "LAST_ADMIN_PROTECTED", message: "不能禁用最后一个有效管理员" }, 409)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='status-21']").trigger("click")
    await flushPromises()

    expect(error).toHaveBeenCalledWith("不能禁用最后一个有效管理员")
    expect(wrapper.text()).toContain("目录操作员")
    wrapper.unmount()
  })

  it("空状态解释账号来源并提供创建本地账号入口", async () => {
    const fetch = routeFetch((url) => {
      if (url.startsWith("/api/v1/web/admin/users?")) return response(listBody([]))
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("尚无平台账号")
    expect(wrapper.text()).toContain("本地账号由管理员创建；AD 账号在首次成功登录后进入台账")
    expect(wrapper.find("[data-testid='empty-create-local-user']").exists()).toBe(true)
    wrapper.unmount()
  })

  it("筛选无结果时区分空台账并给出清除筛选入口", async () => {
    const fetch = routeFetch((url) => {
      if (url.startsWith("/api/v1/web/admin/users?")) return response(listBody([]))
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain("尚无平台账号")

    await wrapper.get("[data-testid='user-filter-keyword']").setValue("不存在的用户")
    await wrapper.get("form.user-filter-bar").trigger("submit")
    await flushPromises()

    expect(wrapper.text()).toContain("没有符合条件的账号")
    expect(wrapper.text()).not.toContain("尚无平台账号")

    await wrapper.get("[data-testid='clear-user-filters']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("尚无平台账号")
    expect((wrapper.get("[data-testid='user-filter-keyword']").element as HTMLInputElement).value).toBe("")
    wrapper.unmount()
  })

  it("提交前即时拦截不合规用户名与临时密码，提交按钮保持禁用", async () => {
    const fetch = routeFetch()
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const created = () => fetch.mock.calls.filter(([url]) => url === "/api/v1/web/admin/users/local").length

    await wrapper.get("[data-testid='create-local-user']").trigger("click")
    const save = () => wrapper.get("[data-testid='save-local-user']")
    expect(save().attributes("disabled")).toBeDefined()

    await wrapper.get("[data-testid='create-username']").setValue("x")
    await wrapper.get("[data-testid='create-display-name']").setValue("新用户")
    await wrapper.get("[data-testid='create-password']").setValue("Temporary@123")
    expect(save().attributes("disabled")).toBeDefined()
    expect(wrapper.get("[data-testid='create-precheck']").text()).toContain("用户名 3–64 位合规")
    expect(wrapper.text()).toContain("本地用户名必须为 3–64 位字母、数字、点、下划线或短横线")
    expect(created()).toBe(0)

    await wrapper.get("[data-testid='create-username']").setValue("new.user")
    await wrapper.get("[data-testid='create-password']").setValue("short")
    expect(save().attributes("disabled")).toBeDefined()
    expect(wrapper.text()).toContain("密码长度必须为 12–128 位")
    expect(created()).toBe(0)

    await wrapper.get("[data-testid='create-password']").setValue("New.user@12345")
    expect(save().attributes("disabled")).toBeDefined()
    expect(wrapper.text()).toContain("密码不能包含用户名")
    expect(created()).toBe(0)

    await wrapper.get("[data-testid='create-password']").setValue("Valid@Pass123")
    expect(save().attributes("disabled")).toBeUndefined()
    await save().trigger("click")
    await flushPromises()
    expect(created()).toBe(1)
    wrapper.unmount()
  })

  it("认证源、角色与状态 seg 点选即重查并映射服务端参数", async () => {
    const fetch = routeFetch()
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(UserView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const listCalls = () =>
      fetch.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith("/api/v1/web/admin/users?"))

    await wrapper.get("[data-testid='user-provider-local']").trigger("click")
    await flushPromises()
    expect(listCalls().at(-1)).toContain("provider_code=local")
    expect(wrapper.get("[data-testid='user-provider-local']").classes()).toContain("on")

    await wrapper.get("[data-testid='user-role-admin']").trigger("click")
    await flushPromises()
    expect(listCalls().at(-1)).toContain("role=admin")

    await wrapper.get("[data-testid='user-status-disabled']").trigger("click")
    await flushPromises()
    expect(listCalls().at(-1)).toContain("status=0")

    await wrapper.get("[data-testid='user-reset']").trigger("click")
    await flushPromises()
    const last = listCalls().at(-1) ?? ""
    expect(last).not.toContain("provider_code=")
    expect(last).not.toContain("role=")
    expect(last).not.toContain("status=")
    wrapper.unmount()
  })
})
