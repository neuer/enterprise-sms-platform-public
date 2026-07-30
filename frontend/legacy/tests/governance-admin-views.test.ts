import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import AppManagementView from "../src/views/AppManagementView.vue"
import BlacklistView from "../src/views/BlacklistView.vue"
import SensitiveWordView from "../src/views/SensitiveWordView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => name === "content-length" && body === undefined ? "0" : null },
    json: async () => body,
  }
}

const app = {
  id: 1,
  name: "app-iam",
  dept: "平台技术部",
  allowed_categories: ["verify"],
  default_sign: "青鸾平台",
  daily_quota: 10000,
  rate_limit_per_min: 60,
  blacklist_check: true,
  freq_override: null,
  callback_url: null,
  callback_report_enabled: false,
  status: 1,
}

describe("管理员治理页面", () => {
  it("应用管理暴露 CRUD 与三种密钥生命周期操作", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/rotate-key")) {
        return response({ api_key: "once-only-key", old_key_expires_at: "2026-07-13T08:00:00+08:00" })
      }
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("app-iam")
    expect(wrapper.find(".app-management-table").exists()).toBe(true)
    expect(wrapper.find(".app-management-mobile-list").exists()).toBe(true)
    expect(wrapper.text()).toContain("旧 Key 宽限期 72 小时")
    expect(wrapper.text()).not.toContain("旧 Key 24h 并行有效")
    expect(wrapper.get("[data-testid='mobile-edit-app-1']").text()).toContain("编辑")
    expect(wrapper.get("[data-testid='new-app']").text()).toContain("新建应用")
    expect(wrapper.get("[data-testid='edit-app-1']").text()).toContain("编辑")
    expect(wrapper.get("[data-testid='revoke-key-1']").text()).toContain("作废旧 Key")
    expect(wrapper.get("[data-testid='rotate-callback-1']").text()).toContain("轮换回调密钥")
    expect(wrapper.get("[data-testid='disable-app-1']").text()).toContain("停用")
    await wrapper.get("[data-testid='rotate-key-1']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("once-only-key")
    await wrapper.get("[data-testid='secret-close']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).not.toContain("once-only-key")
    expect(wrapper.find(".one-time-secret").text()).toBe("")
    expect(wrapper.findComponent({ name: "ElDialog" }).props("destroyOnClose")).toBe(true)
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/admin/apps/1/rotate-key",
      expect.objectContaining({ method: "POST" }),
    )
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("轮换 API Key 需二次确认并在桌面与移动端阻止重复提交", async () => {
    const otherApp = { ...app, id: 2, name: "app-oa" }
    let resolveRotation!: (value: ReturnType<typeof response>) => void
    const rotationResponse = new Promise<ReturnType<typeof response>>((resolve) => {
      resolveRotation = resolve
    })
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/rotate-key")) return rotationResponse
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      return response([app, otherApp])
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const desktopButton = wrapper.get("[data-testid='rotate-key-1']")
    const mobileButton = wrapper.get("[data-testid='mobile-rotate-key-1']")
    const otherDesktopButton = wrapper.get("[data-testid='rotate-key-2']")
    const otherMobileButton = wrapper.get("[data-testid='mobile-rotate-key-2']")
    const callbackButton = wrapper.get("[data-testid='rotate-callback-1']")
    const mobileCallbackButton = wrapper.get("[data-testid='mobile-rotate-callback-1']")
    const newAppButton = wrapper.get("[data-testid='new-app']")
    await desktopButton.trigger("click")
    await flushPromises()

    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("新 Key 仅展示一次"),
      "确认轮换 API Key",
      expect.objectContaining({
        type: "warning",
        confirmButtonText: "确认轮换",
        cancelButtonText: "取消",
      }),
    )
    expect(desktopButton.attributes("disabled")).toBeDefined()
    expect(mobileButton.attributes("disabled")).toBeDefined()
    expect(otherDesktopButton.attributes("disabled")).toBeDefined()
    expect(otherMobileButton.attributes("disabled")).toBeDefined()
    expect(callbackButton.attributes("disabled")).toBeDefined()
    expect(mobileCallbackButton.attributes("disabled")).toBeDefined()
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await otherMobileButton.trigger("click")
    await callbackButton.trigger("click")
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/rotate-key"))).toHaveLength(1)
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/rotate-callback-secret"))).toHaveLength(0)

    resolveRotation(response({
      api_key: "current-final-key",
      old_key_expires_at: "2026-07-13T08:00:00+08:00",
    }))
    await flushPromises()

    expect(document.body.textContent).toContain("这是当前最终 API Key")
    expect(document.body.textContent).toContain("复制并安全保存")
    expect(document.body.textContent).toContain("旧 Key 宽限期至 2026-07-13T08:00:00+08:00")
    expect(otherDesktopButton.attributes("disabled")).toBeDefined()
    expect(otherMobileButton.attributes("disabled")).toBeDefined()
    expect(callbackButton.attributes("disabled")).toBeDefined()
    expect(mobileCallbackButton.attributes("disabled")).toBeDefined()
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='secret-close']").trigger("click")
    await flushPromises()
    expect(desktopButton.attributes("disabled")).toBeUndefined()
    expect(mobileButton.attributes("disabled")).toBeUndefined()
    expect(otherDesktopButton.attributes("disabled")).toBeUndefined()
    expect(otherMobileButton.attributes("disabled")).toBeUndefined()
    expect(callbackButton.attributes("disabled")).toBeUndefined()
    expect(mobileCallbackButton.attributes("disabled")).toBeUndefined()
    expect(newAppButton.attributes("disabled")).toBeUndefined()

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("取消 API Key 轮换确认不产生写请求", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='rotate-key-1']").trigger("click")
    await flushPromises()

    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/rotate-key"))).toHaveLength(0)
    expect(wrapper.get("[data-testid='rotate-key-1']").attributes("disabled")).toBeUndefined()

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("停用应用在桌面端和移动端提供启用操作并可恢复为启用状态", async () => {
    const disabledApp = { ...app, id: 2, name: "app-oa", status: 0 as const }
    let apps = [app, disabledApp]
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/apps/2") && init?.method === "PUT") {
        const enabledApp = { ...disabledApp, status: 1 as const }
        apps = [app, enabledApp]
        return response(enabledApp)
      }
      return response(apps)
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.find("[data-testid='enable-app-1']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='disable-app-1']").text()).toContain("停用")
    expect(wrapper.get("[data-testid='enable-app-2']").text()).toContain("启用")
    expect(wrapper.find("[data-testid='disable-app-2']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='mobile-enable-app-2']").text()).toContain("启用")
    expect(wrapper.find("[data-testid='mobile-disable-app-2']").exists()).toBe(false)

    await wrapper.get("[data-testid='enable-app-2']").trigger("click")
    await flushPromises()

    const updateCall = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith("/admin/apps/2") && init?.method === "PUT",
    )
    expect(updateCall).toBeDefined()
    expect(JSON.parse(String(updateCall?.[1]?.body))).toEqual({
      dept: disabledApp.dept,
      allowed_categories: disabledApp.allowed_categories,
      default_sign: disabledApp.default_sign,
      daily_quota: disabledApp.daily_quota,
      rate_limit_per_min: disabledApp.rate_limit_per_min,
      blacklist_check: disabledApp.blacklist_check,
      freq_override: disabledApp.freq_override,
      callback_url: disabledApp.callback_url,
      callback_report_enabled: disabledApp.callback_report_enabled,
      status: 1,
    })
    expect(wrapper.find("[data-testid='enable-app-2']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='disable-app-2']").text()).toContain("停用")

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("频控覆盖只接受三个文档键和正整数", async () => {
    const fetch = vi.fn().mockResolvedValue(response([app]))
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    await wrapper.get("[data-testid='new-app']").trigger("click")
    const textInputs = wrapper.findAll("input[type='text']")
    await textInputs[0].setValue("new-app")
    await textInputs[1].setValue("平台部")

    const overrideInput = wrapper.get("[data-testid='freq-override']")
    expect(overrideInput.attributes("placeholder")).toContain("verify_per_minute")
    expect(overrideInput.attributes("placeholder")).not.toContain("verify_minute")

    for (const invalid of [
      '{"verify_minute":2}',
      '{"verify_per_minute":true}',
      '{"verify_per_minute":0}',
      '{"verify_per_minute":1.5}',
    ]) {
      await overrideInput.setValue(invalid)
      await wrapper.get("[data-testid='save-app']").trigger("click")
      await flushPromises()
      expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0)
    }

    await overrideInput.setValue('{"verify_per_minute":2}')
    await wrapper.get("[data-testid='save-app']").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1)

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("黑名单只展示掩码并支持批量添加和删除", async () => {
    const item = { phone_hmac: "a".repeat(64), phone_mask: "138****8000", source: "manual", remark: "投诉", created_at: null }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response([item]))
      .mockResolvedValueOnce(response({ added: 1, items: [item] }))
      .mockResolvedValueOnce(response([item]))
      .mockResolvedValueOnce(response(undefined, 204))
      .mockResolvedValueOnce(response([]))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(BlacklistView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.find(".blacklist-mobile-list").exists()).toBe(true)
    expect(wrapper.get("[data-testid='mobile-blacklist-delete-aaaaaaaa']").text()).toContain("移除")
    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.text()).not.toContain("13800138000")
    await wrapper.get("[data-testid='blacklist-phones']").setValue("13800138000\n13900139000")
    await wrapper.get("[data-testid='blacklist-add']").trigger("click")
    await flushPromises()
    expect(JSON.parse(String(fetch.mock.calls[1][1].body))).toEqual({
      phones: ["13800138000", "13900139000"], source: "manual", remark: null,
    })
    await wrapper.get("[data-testid='blacklist-delete-aaaaaaaa']").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[3][0]).toBe(`/api/v1/web/admin/blacklist/${"a".repeat(64)}`)
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("敏感词支持批量添加、删除并在本页切换 block/audit 策略", async () => {
    const config = { key: "sensitive_action", value: "block", value_type: "str", description: "敏感词策略", group: "内容治理", sensitive: false, configured: true, beat_restart_required: false, updated_by: null, updated_at: null }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/configs") && init?.method === "PUT") return response([config])
      if (url.endsWith("/admin/configs")) return response([config])
      if (url.endsWith("/admin/sensitive-words") && init?.method === "POST") return response([{ id: 2, word: "诈骗" }])
      if (url.includes("/admin/sensitive-words/")) return response(undefined, 204)
      return response([{ id: 1, word: "测试敏感词" }])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(SensitiveWordView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='sensitive-policy']").classes()).toContain("sensitive-policy")
    expect(wrapper.text()).toContain("测试敏感词")
    await wrapper.get("[data-testid='sensitive-words-input']").setValue("诈骗\n赌博")
    await wrapper.get("[data-testid='sensitive-words-add']").trigger("click")
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/admin/sensitive-words",
      expect.objectContaining({ body: JSON.stringify({ words: ["诈骗", "赌博"] }) }),
    )
    const policy = wrapper.findComponent({ name: "ElSelect" })
    policy.vm.$emit("update:modelValue", "audit")
    policy.vm.$emit("change", "audit")
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/admin/configs",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ items: [{ key: "sensitive_action", value: "audit" }] }) }),
    )
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
