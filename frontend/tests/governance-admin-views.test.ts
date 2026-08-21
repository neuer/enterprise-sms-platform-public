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
  api_key_prefix: "sk-7f3a9c2e",
  old_key_prefix: null,
  old_key_expires_at: null,
  callback_secret_configured: true,
  created_at: "2026-04-02T10:18:44+08:00",
}

/** 今日用量联查的空响应（stat_daily 无记录）。 */
const EMPTY_USAGE = {
  granularity: "day",
  group_by: "app",
  category: "all",
  start: "2026-08-21",
  end: "2026-08-21",
  can_export_decrypted: false,
  summary: { total: 0, total_segments: 0, delivered: 0, failed: 0, unknown: 0, success_rate: 0 },
  dim_summary: [],
  items: [],
}

describe("管理员治理页面", () => {
  it("应用管理以筛选条加账本表格呈现，密钥操作收进详情抽屉", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/rotate-key")) {
        return response({ api_key: "once-only-key", old_key_expires_at: "2026-07-13T08:00:00+08:00" })
      }
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
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
    expect(wrapper.find("[data-testid='app-table']").exists()).toBe(true)
    expect(wrapper.find(".apps-filter-bar").exists()).toBe(true)
    expect(wrapper.text()).toContain("接口全量返回 · 前端过滤")
    expect(wrapper.find(".app-card-grid").exists()).toBe(false)
    expect(wrapper.find(".managed-app-card").exists()).toBe(false)
    expect(wrapper.text()).toContain("共 1 个应用 · 启用 1 · 停用 0")
    expect(wrapper.text()).toContain("读写：admin · 今日消耗来自 stat_daily 联查")
    expect(wrapper.get("[data-testid='new-app']").text()).toContain("新建应用")
    // 行内只留「详情」，轮换/作废/停用全部收进详情抽屉
    expect(wrapper.find("[data-testid='rotate-key-1']").exists()).toBe(false)
    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("#1 · 平台技术部 · 创建于 2026-04-02 10:18:44")
    expect(wrapper.get("[data-testid='edit-app-1']").text()).toContain("编辑配置")
    expect(wrapper.get("[data-testid='demo-script-1']").text()).toContain("接入示例")
    expect(wrapper.get("[data-testid='rotate-callback-1']").text()).toContain("轮换回调密钥")
    expect(wrapper.get("[data-testid='disable-app-1']").text()).toContain("停用应用")
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

  it("密钥列呈现前缀与三态标签，宽限入口仅旧 Key 存在时出现", async () => {
    const graceApp = {
      ...app,
      id: 2,
      name: "app-mall",
      api_key_prefix: "sk-mall88ab",
      old_key_prefix: "sk-old99cd",
      old_key_expires_at: new Date(Date.now() + 41.2 * 3_600_000).toISOString(),
      callback_url: "https://gw-mall.example.internal/sms/callback",
      callback_report_enabled: true,
    }
    const disabledApp = {
      ...app,
      id: 3,
      name: "app-legacy",
      status: 0 as const,
      api_key_prefix: "revoked0",
    }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/revoke-old-key") && init?.method === "POST") return response(undefined, 204)
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
      return response([app, graceApp, disabledApp])
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("sk-7f3a9c2e••••")
    expect(wrapper.text()).toContain("单 Key 运行")
    expect(wrapper.text()).toContain("sk-mall88ab••••")
    expect(wrapper.text()).toContain("旧 Key 宽限 · 余 42h")
    expect(wrapper.text()).toContain("已随停用吊销")
    expect(wrapper.text()).not.toContain("revoked0••••")
    expect(wrapper.text()).toContain("gw-mall.example.internal/sms/callback")
    expect(wrapper.text()).toContain("明细回调 开启 · 密钥已配置")
    expect(wrapper.text()).toContain("共 3 个应用 · 启用 2 · 停用 1")

    // 无旧 Key 的应用不提供作废入口
    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    expect(wrapper.find("[data-testid='revoke-old-key-1']").exists()).toBe(false)

    // 宽限期中的应用提供作废入口，确认提示旧前缀与原到期时间
    await wrapper.get("[data-testid='app-detail-2']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='revoke-old-key-2']").trigger("click")
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("sk-old99cd••••"),
      "立即作废旧 Key？",
      expect.objectContaining({ type: "warning", confirmButtonText: "确认作废" }),
    )
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("到期，作废后立即失效"),
      "立即作废旧 Key？",
      expect.objectContaining({ type: "warning" }),
    )
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/revoke-old-key"))).toHaveLength(1)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("详情抽屉呈现运行概览、密钥回调与策略三段事实", async () => {
    const usage = {
      ...EMPTY_USAGE,
      dim_summary: [
        {
          dim_value: "1",
          dim_label: "app-iam",
          total: 41203,
          total_segments: 41203,
          delivered: 40629,
          failed: 574,
          unknown: 0,
          success_rate: 0.9861,
        },
      ],
    }
    const configured = {
      ...app,
      daily_quota: 50000,
      blacklist_check: false,
      freq_override: { verify_per_minute: 2, market_per_day: 1 },
      callback_url: "https://callback.internal/sms",
      callback_report_enabled: true,
    }
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response(usage)
      return response([configured])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    // 列表今日消耗列：消耗 / 配额 + 进度条（82.4% > 80% 琥珀）
    expect(wrapper.text()).toContain("41,203")
    expect(wrapper.text()).toContain("/ 50,000")
    const bar = wrapper.get(".apps-quota-bar i")
    expect(bar.classes()).toContain("warn")

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()

    expect(wrapper.text()).toContain("运行概览 · 今日")
    expect(wrapper.text()).toContain("成功率 98.6%（delivered/(delivered+failed)）")
    expect(wrapper.text()).toContain("每分钟限流")
    expect(wrapper.text()).toContain("频控覆盖")
    expect(wrapper.text()).toContain("验证码 2/分")
    expect(wrapper.text()).toContain("营销 1/日")
    expect(wrapper.text()).toContain("密钥与回调")
    expect(wrapper.text()).toContain("https://callback.internal/sms")
    expect(wrapper.text()).toContain("已配置")
    expect(wrapper.text()).toContain("策略")
    expect(wrapper.text()).toContain("默认签名")
    expect(wrapper.text()).toContain("青鸾平台")
    expect(wrapper.text()).toContain("黑名单检查")
    expect(wrapper.text()).toContain("关闭")
    expect(wrapper.text()).toContain("来源 IP 白名单")
    expect(wrapper.text()).toContain("全网放行")

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("今日用量联查失败时单元格显示占位符而不拖垮列表", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response({ code: "INTERNAL_ERROR" }, 500)
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("app-iam")
    expect(wrapper.text()).toContain("—")
    expect(wrapper.find(".apps-quota-bar").exists()).toBe(false)

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("今日用量统计暂不可用")

    wrapper.unmount()
    vi.unstubAllGlobals()
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
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
      return response([app, otherApp])
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    const rotateButton = wrapper.get("[data-testid='rotate-key-1']")
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
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("72 小时宽限期"),
      "确认轮换 API Key",
      expect.objectContaining({ type: "warning" }),
    )
    expect(rotateButton.attributes("disabled")).toBeDefined()
    expect(callbackButton.attributes("disabled")).toBeDefined()
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await callbackButton.trigger("click")
    // 轮换进行中新建应用与另一应用的轮换入口都被单飞守卫禁用
    await wrapper.get("[data-testid='app-detail-2']").trigger("click")
    await flushPromises()
    const otherRotateButton = wrapper.get("[data-testid='rotate-key-2']")
    expect(otherRotateButton.attributes("disabled")).toBeDefined()
    await otherRotateButton.trigger("click")
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
    expect(newAppButton.attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='secret-close']").trigger("click")
    await flushPromises()
    expect(wrapper.get("[data-testid='rotate-key-2']").attributes("disabled")).toBeUndefined()
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
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='rotate-key-1']").trigger("click")
    await flushPromises()

    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/rotate-key"))).toHaveLength(0)
    expect(wrapper.get("[data-testid='rotate-key-1']").attributes("disabled")).toBeUndefined()

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("取消回调密钥轮换确认不产生写请求", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
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
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
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

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
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

  it("停用确认逐条列出后果，启用先取权威配置再仅改状态", async () => {
    const disabledApp = {
      ...app,
      id: 2,
      name: "app-oa",
      status: 0 as const,
      api_key_prefix: "revoked0",
      old_key_prefix: null,
      old_key_expires_at: null,
    }
    let apps = [app, disabledApp]
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/apps/2") && init?.method === "PUT") {
        const enabledApp = { ...disabledApp, status: 1 as const }
        apps = [app, enabledApp]
        return response(enabledApp)
      }
      if (url.endsWith("/admin/apps/2") && init?.method === "GET") return response(disabledApp)
      if (url.endsWith("/admin/configs")) {
        return response([{ key: "key_grace_hours", value: "72" }])
      }
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
      return response(apps)
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    // 停用确认逐条列出后果
    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    expect(wrapper.find("[data-testid='enable-app-1']").exists()).toBe(false)
    await wrapper.get("[data-testid='disable-app-1']").trigger("click")
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ type: "div" }),
      "停用应用 app-iam？",
      expect.objectContaining({ type: "warning", confirmButtonText: "确认停用" }),
    )
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/admin/apps/1"))).toHaveLength(1)

    // 停用应用的详情抽屉提供启用入口
    await wrapper.get("[data-testid='app-detail-2']").trigger("click")
    await flushPromises()
    expect(wrapper.find("[data-testid='disable-app-2']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='enable-app-2']").text()).toContain("启用")
    await wrapper.get("[data-testid='enable-app-2']").trigger("click")
    await flushPromises()

    // 启用流程：先 GET 权威配置，再 PUT 仅改 status
    const calls = fetch.mock.calls
    const order = fetch.mock.invocationCallOrder
    const getIndex = calls.findIndex(
      ([url, init]) => String(url).endsWith("/admin/apps/2") && init?.method === "GET",
    )
    const putIndex = calls.findIndex(
      ([url, init]) => String(url).endsWith("/admin/apps/2") && init?.method === "PUT",
    )
    expect(getIndex).toBeGreaterThanOrEqual(0)
    expect(putIndex).toBeGreaterThanOrEqual(0)
    expect(order[getIndex]).toBeLessThan(order[putIndex])
    expect(JSON.parse(String(calls[putIndex]?.[1]?.body))).toEqual({
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

  it("新建抽屉按三段分组并对危险默认值就地警示", async () => {
    const fetch = vi.fn().mockResolvedValue(response([app]))
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    await wrapper.get("[data-testid='new-app']").trigger("click")
    await flushPromises()

    const drawer = wrapper.get(".apps-editor-drawer")
    expect(drawer.text()).toContain("基本信息")
    expect(drawer.text()).toContain("配额与策略")
    expect(drawer.text()).toContain("安全与回调")
    expect(drawer.text()).toContain("日配额为 0 表示不限量")
    expect(drawer.text()).toContain("白名单为空表示全网放行")
    expect(drawer.text()).toContain("保存即记审计（app_create）")
    expect(drawer.get("[data-testid='save-app']").text()).toContain("创建应用")

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("默认签名仅从已通过签名中选择，遗留值与清单失败均有明确呈现", async () => {
    const signs = [
      { id: 1, name: "青鸾平台", vendor_sign_id: "sgn-1", vendor_state: "approved", vendor_reject_reason: null },
      { id: 2, name: "青鸾商城", vendor_sign_id: null, vendor_state: "pending", vendor_reject_reason: null },
    ]
    const legacyApp = { ...app, id: 2, name: "app-legacy", default_sign: "旧签名" }
    const fetch = vi.fn(async (url: string) => {
      if (String(url).endsWith("/signs")) return response(signs)
      return response([app, legacyApp])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    // 编辑 default_sign 为已通过签名的应用：选项只含已通过，pending 不出现
    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='edit-app-1']").trigger("click")
    await flushPromises()
    const select = wrapper.get("[data-testid='default-sign-select']")
    expect(select.attributes("placeholder")).toBeUndefined()
    await select.trigger("click")
    await flushPromises()
    const optionTexts = Array.from(document.body.querySelectorAll(".el-select-dropdown__item"))
      .map((el) => el.textContent ?? "")
    expect(optionTexts.some((text) => text.includes("【青鸾平台】"))).toBe(true)
    expect(optionTexts.some((text) => text.includes("青鸾商城"))).toBe(false)
    // 当前值已通过 → 不出现遗留标注
    expect(optionTexts.some((text) => text.includes("遗留值"))).toBe(false)
    // 关闭下拉与编辑抽屉
    await select.trigger("click")
    await flushPromises()
    await wrapper.find(".apps-editor-drawer .el-drawer__close-btn").trigger("click")
    await flushPromises()

    // 编辑遗留值应用：下拉中遗留项被单独标注
    await wrapper.get("[data-testid='app-detail-2']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='edit-app-2']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='default-sign-select']").trigger("click")
    await flushPromises()
    const legacyOptions = Array.from(document.body.querySelectorAll(".el-select-dropdown__item"))
      .map((el) => el.textContent ?? "")
    expect(legacyOptions.some((text) => text.includes("【旧签名】（未通过审核的遗留值）"))).toBe(true)

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("签名清单加载失败时表单不阻塞并就地提供重试", async () => {
    const fetch = vi.fn(async (url: string) => {
      if (String(url).endsWith("/signs")) return response({ code: "INTERNAL_ERROR" }, 500)
      return response([app])
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AppManagementView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain("app-iam")

    await wrapper.get("[data-testid='new-app']").trigger("click")
    await flushPromises()
    const drawer = wrapper.get(".apps-editor-drawer")
    expect(drawer.text()).toContain("签名清单加载失败")
    await drawer.get("[data-testid='signs-retry']").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/signs")).length).toBeGreaterThanOrEqual(2)

    wrapper.unmount()
    vi.unstubAllGlobals()
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
    await flushPromises()
    const drawer = wrapper.get(".apps-editor-drawer")
    const textInputs = drawer.findAll("input[type='text']")
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
    await flushPromises()
    const drawer = wrapper.get(".apps-editor-drawer")
    const textInputs = drawer.findAll("input[type='text']")
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
      if (String(url).includes("/reports/stats")) return response(EMPTY_USAGE)
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

    await wrapper.get("[data-testid='app-detail-1']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='demo-script-1']").trigger("click")
    await flushPromises()

    expect(document.body.textContent).toContain("接入示例 · app-iam")
    expect(wrapper.find("[data-testid='demo-template-select']").exists()).toBe(true)
    expect(wrapper.get("[data-testid='demo-template-info']").text()).toContain("尊敬的{1}，您的工单{2}已创建")
    const curlBody = wrapper.get("[data-testid='demo-script-body-curl']")
    // 必须用双引号包裹，shell 才会展开 $SMS_API_KEY；单引号会把字面量发给服务端。
    expect(curlBody.text()).toContain('-H "X-Api-Key: $SMS_API_KEY"')
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
