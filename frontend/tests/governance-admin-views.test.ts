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
  allowed_ips: [],
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
    expect(wrapper.find(".app-card-grid").exists()).toBe(true)
    expect(wrapper.find(".managed-app-card").exists()).toBe(true)
    expect(wrapper.find(".app-management-table").exists()).toBe(false)
    expect(wrapper.find(".app-management-mobile-list").exists()).toBe(false)
    expect(wrapper.text()).toContain("旧 Key 宽限期 72 小时")
    expect(wrapper.text()).not.toContain("旧 Key 24h 并行有效")
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

  it("轮换 API Key 需二次确认并在请求中阻止重复提交", async () => {
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

    const rotateButton = wrapper.get("[data-testid='rotate-key-1']")
    const otherRotateButton = wrapper.get("[data-testid='rotate-key-2']")
    const callbackButton = wrapper.get("[data-testid='rotate-callback-1']")
    const newAppButton = wrapper.get("[data-testid='new-app']")
    await rotateButton.trigger("click")
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
    expect(rotateButton.attributes("disabled")).toBeDefined()
    expect(otherRotateButton.attributes("disabled")).toBeDefined()
    expect(callbackButton.attributes("disabled")).toBeDefined()
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await otherRotateButton.trigger("click")
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
    expect(document.body.textContent).toContain("旧 Key 宽限期至 2026-07-13 08:00:00")
    expect(otherRotateButton.attributes("disabled")).toBeDefined()
    expect(callbackButton.attributes("disabled")).toBeDefined()
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='secret-close']").trigger("click")
    await flushPromises()
    expect(rotateButton.attributes("disabled")).toBeUndefined()
    expect(otherRotateButton.attributes("disabled")).toBeUndefined()
    expect(callbackButton.attributes("disabled")).toBeUndefined()
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

  it("卡片呈现黑名单、频控覆盖与回调投递策略摘要", async () => {
    const configured = {
      ...app,
      daily_quota: 0,
      blacklist_check: false,
      freq_override: { verify_per_minute: 2, market_per_day: 1 },
      callback_url: "https://callback.internal/sms",
      callback_report_enabled: true,
    }
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      return response([configured])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const card = wrapper.get(".managed-app-card")
    expect(card.text()).toContain("不限量")
    expect(card.text()).toContain("黑名单检查")
    expect(card.text()).toContain("关闭")
    expect(card.text()).toContain("频控覆盖")
    expect(card.text()).toContain("验证码 2/分")
    expect(card.text()).toContain("营销 1/日")
    expect(card.text()).toContain("来源白名单")
    expect(card.text()).toContain("不限")
    expect(card.text()).toContain("https://callback.internal/sms")
    expect(card.text()).toContain("消息级回调")
    expect(card.text()).toContain("开启")

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("取消回调密钥轮换确认不产生写请求", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='rotate-callback-1']").trigger("click")
    await flushPromises()

    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("回调密钥"),
      "确认轮换回调密钥",
      expect.objectContaining({ type: "warning" }),
    )
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/rotate-callback-secret"))).toHaveLength(0)
    expect(wrapper.get("[data-testid='rotate-callback-1']").attributes("disabled")).toBeUndefined()

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("一次性密钥弹窗支持复制到剪贴板，关闭后清空", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (url.endsWith("/rotate-callback-secret")) {
        return response({ callback_secret: "cb-secret-once" })
      }
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true })
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true })
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='rotate-callback-1']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("cb-secret-once")

    await wrapper.get("[data-testid='secret-copy']").trigger("click")
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith("cb-secret-once")

    await wrapper.get("[data-testid='secret-close']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).not.toContain("cb-secret-once")

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
      allowed_ips: disabledApp.allowed_ips,
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

  it("来源 IP 白名单按行解析并随应用创建提交", async () => {
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return response({ id: 2, api_key: "once-only-key", callback_secret: null })
      }
      return response([app])
    })
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
    await wrapper.get("[data-testid='allowed-ips-input']").setValue("203.0.113.7\n10.0.0.0/8 \n")
    await wrapper.get("[data-testid='save-app']").trigger("click")
    await flushPromises()

    const createCall = fetch.mock.calls.find(([, init]) => init?.method === "POST")
    expect(createCall).toBeDefined()
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual(
      expect.objectContaining({
        name: "new-app",
        allowed_ips: ["203.0.113.7", "10.0.0.0/8"],
      }),
    )
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("接入示例展示多语言 demo 脚本并支持复制", async () => {
    const approvedTemplate = {
      id: 12,
      name: "工单通知",
      content: "尊敬的{1}，您的工单{2}已创建",
      var_specs: [{ pos: 1, max_len: 10 }, { pos: 2, max_len: 20 }],
      dept: "平台技术部",
      vendor_template_id: "T123",
      vendor_state: "approved",
      vendor_reject_reason: null,
    }
    const fetch = vi.fn(async (url: string) => {
      if (String(url).endsWith("/web/templates")) return response([approvedTemplate])
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true })
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true })
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='demo-script-1']").trigger("click")
    await flushPromises()

    expect(document.body.textContent).toContain("接入示例 · app-iam")
    expect(wrapper.find("[data-testid='demo-template-select']").exists()).toBe(true)
    expect(wrapper.get("[data-testid='demo-template-info']").text()).toContain("尊敬的{1}，您的工单{2}已创建")
    const curlBody = wrapper.get("[data-testid='demo-script-body-curl']")
    expect(curlBody.text()).toContain("X-Api-Key")
    expect(curlBody.text()).toContain('"template_id":12')
    expect(curlBody.text()).toContain('"template_params":["示例参数1","示例参数2"]')
    expect(curlBody.text()).toContain("138****8000")

    const tabs = wrapper.findAll(".el-tabs__item")
    const pythonTab = tabs.find((tab) => tab.text().includes("Python"))
    expect(pythonTab).toBeDefined()
    await pythonTab!.trigger("click")
    await flushPromises()
    const pythonBody = wrapper.get("[data-testid='demo-script-body-python']")
    expect(pythonBody.text()).toContain("import requests")
    expect(pythonBody.text()).toContain('os.environ["SMS_API_KEY"]')
    expect(pythonBody.text()).toContain("send_template([\"138****8000\"], 12, [\"示例参数1\", \"示例参数2\"]")

    await wrapper.get("[data-testid='demo-copy']").trigger("click")
    await flushPromises()
    expect(writeText).toHaveBeenCalled()
    expect(writeText.mock.calls[0][0]).toContain("template_id")
    expect(writeText.mock.calls[0][0]).toContain("SMS_API_KEY")

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("黑名单只展示掩码并支持批量添加和删除", async () => {
    const item = { phone_hmac: "a".repeat(64), phone_mask: "138****8000", source: "manual", remark: "投诉", created_at: null }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [item] }))
      .mockResolvedValueOnce(response({ added: 1, updated: 0, items: [item] }))
      .mockResolvedValueOnce(response({ total: 1, items: [item] }))
      .mockResolvedValueOnce(response(undefined, 204))
      .mockResolvedValueOnce(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(BlacklistView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.find(".blacklist-mobile-list").exists()).toBe(true)
    expect(String(fetch.mock.calls[0][0])).toContain("/api/v1/web/admin/blacklist?page=1&size=20")
    expect(wrapper.get("[data-testid='mobile-blacklist-delete-aaaaaaaa']").text()).toContain("移除")
    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.find(".phone-mask").exists()).toBe(true)
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
    const config = { key: "sensitive_hit_action", value: "block", value_type: "str", description: "敏感词策略", group: "发送策略", sensitive: false, configured: true, beat_restart_required: false, updated_by: null, updated_at: null, default: "block", min_value: null, max_value: null }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/configs") && init?.method === "PUT") return response([config])
      if (url.endsWith("/admin/configs")) return response([config])
      if (url.endsWith("/admin/sensitive-words") && init?.method === "POST") return response({ added: 1, skipped: 0, items: [{ id: 2, word: "诈骗", created_at: null }] })
      if (url.includes("/admin/sensitive-words/")) return response(undefined, 204)
      return response({ total: 1, items: [{ id: 1, word: "测试敏感词", created_at: null }] })
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
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ items: [{ key: "sensitive_hit_action", value: "audit" }] }) }),
    )
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
