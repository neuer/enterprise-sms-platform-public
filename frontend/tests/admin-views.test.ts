import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import AuditView from "../src/views/AuditView.vue"
import ConfigView from "../src/views/ConfigView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

const configs = [
  { key: "vendor_qps", value: "5", value_type: "int", description: "厂商 QPS", group: "运行调度", sensitive: false, configured: true, beat_restart_required: false, updated_by: null, updated_at: null, default: "5", min_value: null, max_value: 1000 },
  { key: "report_poll_seconds", value: "60", value_type: "int", description: "报告轮询", group: "运行调度", sensitive: false, configured: true, beat_restart_required: true, updated_by: null, updated_at: null, default: "60", min_value: 10, max_value: 3600 },
  { key: "alert_wecom_webhook", value: null, value_type: "str", description: "企微 Webhook", group: "告警通知", sensitive: true, configured: true, beat_restart_required: false, updated_by: "admin01", updated_at: "2026-07-12T08:00:00+08:00", default: "", min_value: null, max_value: null },
]

const adConfig = {
  server: "ldaps://ad.example.com:636",
  base_dn: "DC=example,DC=com",
  bind_dn: "CN=sms-bind,OU=Service,DC=example,DC=com",
  user_search_filter: "(sAMAccountName={username})",
  username_attribute: "sAMAccountName",
  display_name_attribute: "displayName",
  dept_attribute: "department",
  subject_attribute: "objectGUID",
  group_attribute: "memberOf",
  connect_timeout_s: 5,
  receive_timeout_s: 8,
}

const adProvider = {
  code: "ad",
  name: "企业 AD",
  kind: "ldap",
  enabled: false,
  draft_config: adConfig,
  active_config: null,
  draft_version: 3,
  tested_version: null,
  active_version: null,
  last_tested_at: null,
  last_test_status: null,
  bind_secret_available: true,
  ca_available: true,
}

const roleMappings = {
  mappings: [
    {
      external_group: "CN=SMS-Operators,OU=Groups,DC=example,DC=com",
      role: "operator",
    },
  ],
}

function configFetch(
  overrides?: (url: string, init: RequestInit) => ReturnType<typeof response> | undefined,
) {
  return vi.fn().mockImplementation((url: string, init: RequestInit = {}) => {
    const overridden = overrides?.(url, init)
    if (overridden) return Promise.resolve(overridden)
    if (url === "/api/v1/web/admin/auth-providers/ad") return Promise.resolve(response(adProvider))
    if (url === "/api/v1/web/admin/auth-providers/ad/role-mappings") {
      return Promise.resolve(response(roleMappings))
    }
    if (url === "/api/v1/web/admin/configs") return Promise.resolve(response(configs))
    return Promise.resolve(response(undefined))
  })
}

const auditPage = {
  items: [{ id: 9, correlation_id: "30000000-0000-4000-8000-000000000009", actor: "admin01", actor_subject_kind: "human", actor_account_id: 1, actor_identity_id: 10, actor_app_id: null, role: "admin", ip: "10.0.0.8", action: "config_update", object_type: "sys_config", object_id: "vendor_qps", before_val: { value: "5", note: "old" }, after_val: { value: "8", enabled: true }, created_at: "2026-07-12T08:00:00+08:00" }],
  total: 1,
  page: 1,
  page_size: 20,
}

function auditFetch() {
  return vi.fn().mockImplementation((url: string) => {
    if (String(url).includes("/admin/audit-logs/actions")) {
      return Promise.resolve(response(["config_update", "user_create"]))
    }
    return Promise.resolve(response(auditPage))
  })
}

function lastAuditQuery(fetch: ReturnType<typeof auditFetch>): string {
  const calls = fetch.mock.calls.filter((call) => String(call[0]).includes("/admin/audit-logs?"))
  return String(calls.at(-1)![0])
}

describe("审计与系统参数", () => {
  it("仅管理员看到运行参数、认证源、真实联调三个配置页签", async () => {
    vi.stubGlobal("fetch", configFetch())
    const pinia = createPinia()
    setActivePinia(pinia)
    const session = useSessionStore()
    session.role = "admin"
    const wrapper = mount(ConfigView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.findAll("[role='tab']").map((tab) => tab.text())).toEqual([
      "运行参数",
      "认证源",
      "真实联调",
    ])
    wrapper.unmount()

    session.role = "viewer"
    const viewer = mount(ConfigView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(viewer.findAll("[role='tab']").map((tab) => tab.text())).toEqual([
      "运行参数",
      "认证源",
    ])
    viewer.unmount()
    vi.unstubAllGlobals()
  })

  it("分组编辑配置并对 beat 参数显示重启确认", async () => {
    const fetch = configFetch((url, init) => {
      if (url === "/api/v1/web/admin/configs" && init.method === "PUT") return response(configs)
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("系统参数")
    expect(wrapper.text()).toContain("由 beat 与 API 在启动时读取")
    expect(wrapper.text()).toContain("修改后需重启两个容器")
    expect(wrapper.text()).toContain("已配置，值不回显")
    await wrapper.get("[data-testid='config-report_poll_seconds'] input").setValue("90")
    await wrapper.findAll("button").find((button) => button.text().includes("清除配置"))!.trigger("click")
    await wrapper.findAll("button").find((button) => button.text().includes("保存变更"))!.trigger("click")
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalled()
    const request = fetch.mock.calls.find(
      ([url, init]) => url === "/api/v1/web/admin/configs" && init.method === "PUT",
    )
    expect(JSON.parse(String(request?.[1].body))).toEqual({
      items: [{ key: "report_poll_seconds", value: "90" }, { key: "alert_wecom_webhook", value: "" }],
    })
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("支持按关键字搜索参数并展示数值范围与重置入口", async () => {
    vi.stubGlobal("fetch", configFetch())
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("范围 10 – 3600 · 默认 60")
    expect(wrapper.findAll(".config-item")).toHaveLength(3)

    await wrapper.get("[data-testid='config-search']").setValue("webhook")
    expect(wrapper.findAll(".config-item")).toHaveLength(1)
    expect(wrapper.text()).toContain("企微 Webhook")
    expect(wrapper.text()).not.toContain("报告轮询")

    await wrapper.get("[data-testid='config-search']").setValue("不存在的参数")
    expect(wrapper.findAll(".config-item")).toHaveLength(0)
    expect(wrapper.text()).toContain("没有匹配的系统参数")

    await wrapper.get("[data-testid='config-search']").setValue("")
    await wrapper.get("[data-testid='config-report_poll_seconds'] input").setValue("90")
    const reset = wrapper.get("[data-testid='config-reset-report_poll_seconds']")
    await reset.trigger("click")
    expect(
      (wrapper.get("[data-testid='config-report_poll_seconds'] input").element as HTMLInputElement).value,
    ).toBe("60")
    expect(wrapper.find("[data-testid='config-reset-report_poll_seconds']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='config-reset-alert_wecom_webhook']").exists()).toBe(false)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("在运行参数之前展示不可变本地源与 AD 非敏感配置状态", async () => {
    vi.stubGlobal("fetch", configFetch())
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text().indexOf("认证源")).toBeLessThan(wrapper.text().indexOf("运行调度"))
    expect(wrapper.get("[data-testid='local-provider']").text()).toContain("始终启用")
    expect(wrapper.get("[data-testid='local-provider']").text()).toContain("系统内置，不可修改")
    expect(wrapper.text()).toContain("AD 当前已禁用")
    expect(wrapper.text()).toContain("草稿版本 v3")
    expect(wrapper.text()).toContain("Bind Secret 已就绪")
    expect(wrapper.text()).toContain("CA 证书已就绪")
    expect(wrapper.find("[data-testid='ad-bind-password']").exists()).toBe(false)
    expect(wrapper.find("input[type='password']").exists()).toBe(true) // 仅现有敏感运行参数
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("编辑 AD 草稿立即标记测试失效并以完整非敏感配置保存", async () => {
    const staleProvider = { ...adProvider, draft_version: 4, tested_version: 3, last_test_status: "success" }
    const fetch = configFetch((url, init) => {
      if (url === "/api/v1/web/admin/auth-providers/ad/draft" && init.method === "PUT") {
        return response(staleProvider)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='ad-server']").setValue("ldaps://new-ad.example.com:636")
    expect(wrapper.text()).toContain("配置已修改，需保存并重新测试")
    expect(wrapper.get("[data-testid='activate-ad']").attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='save-ad-draft']").trigger("click")
    await flushPromises()

    const request = fetch.mock.calls.find(([url]) => url.endsWith("/auth-providers/ad/draft"))
    const body = JSON.parse(String(request?.[1].body))
    expect(body.config.server).toBe("ldaps://new-ad.example.com:636")
    expect(body.config.bind_password).toBeUndefined()
    expect(wrapper.text()).toContain("草稿版本 v4")
    expect(wrapper.text()).toContain("当前草稿尚未通过测试")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("连接测试失败后不可启用，通过当前版本测试后才允许启用", async () => {
    let tests = 0
    const failure = { ...adProvider, tested_version: null, last_test_status: "failed" }
    const success = { ...adProvider, tested_version: 3, last_test_status: "success" }
    const active = { ...success, enabled: true, active_config: adConfig, active_version: 3 }
    let current: Record<string, unknown> = adProvider
    const fetch = configFetch((url) => {
      if (url === "/api/v1/web/admin/auth-providers/ad/test") {
        tests += 1
        current = tests === 1 ? failure : success
        return response({ success: tests === 2, result_code: tests === 1 ? "LDAP_CONNECTION_FAILED" : "OK" })
      }
      if (url === "/api/v1/web/admin/auth-providers/ad") return response(current)
      if (url === "/api/v1/web/admin/auth-providers/ad/activate") {
        current = active
        return response(active)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const error = vi.spyOn(ElMessage, "error")
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='test-ad']").trigger("click")
    await flushPromises()
    expect(error).toHaveBeenCalledWith("连接测试失败：LDAP_CONNECTION_FAILED")
    expect(wrapper.get("[data-testid='activate-ad']").attributes("disabled")).toBeDefined()

    await wrapper.get("[data-testid='test-ad']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("当前草稿测试通过")
    expect(wrapper.get("[data-testid='activate-ad']").attributes("disabled")).toBeUndefined()
    await wrapper.get("[data-testid='activate-ad']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("AD 当前已启用")
    expect(wrapper.text()).toContain("生效版本 v3")
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("AD 已启用时仍允许发布通过测试的新草稿版本", async () => {
    const upgraded = {
      ...adProvider,
      enabled: true,
      active_config: adConfig,
      active_version: 2,
      draft_version: 3,
      tested_version: 3,
      last_test_status: "success",
    }
    vi.stubGlobal("fetch", configFetch((url) => {
      if (url === "/api/v1/web/admin/auth-providers/ad") return response(upgraded)
      return undefined
    }))
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("草稿版本 v3")
    expect(wrapper.text()).toContain("生效版本 v2")
    expect(wrapper.text()).toContain("当前草稿测试通过")
    expect(wrapper.get("[data-testid='activate-ad']").attributes("disabled")).toBeUndefined()
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("禁用 AD 保留配置并可维护目录组角色映射", async () => {
    const enabled = { ...adProvider, enabled: true, active_config: adConfig, active_version: 3, tested_version: 3, last_test_status: "success" }
    const disabled = { ...enabled, enabled: false }
    const fetch = configFetch((url, init) => {
      if (url === "/api/v1/web/admin/auth-providers/ad") return response(enabled)
      if (url === "/api/v1/web/admin/auth-providers/ad/disable") return response(disabled)
      if (url === "/api/v1/web/admin/auth-providers/ad/role-mappings" && init.method === "PUT") {
        return response({ mappings: [{ external_group: "CN=SMS-Admins,OU=Groups,DC=example,DC=com", role: "admin", dept: "平台部" }] })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='disable-ad']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("AD 当前已禁用")
    expect(wrapper.text()).toContain("配置与角色映射均已保留")

    await wrapper.get("[data-testid='mapping-group-0']").setValue("CN=SMS-Admins,OU=Groups,DC=example,DC=com")
    await wrapper.get("[data-testid='mapping-dept-0']").setValue("平台部")
    await wrapper.getComponent("[data-testid='mapping-role-0']").setValue("admin")
    await wrapper.get("[data-testid='save-role-mappings']").trigger("click")
    await flushPromises()
    const request = fetch.mock.calls.find(
      ([url, init]) => url.endsWith("/role-mappings") && init.method === "PUT",
    )
    expect(JSON.parse(String(request?.[1].body))).toEqual({
      mappings: [{ external_group: "CN=SMS-Admins,OU=Groups,DC=example,DC=com", role: "admin", dept: "平台部" }],
    })
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("检索审计并在 drawer 展示载荷差异与同链路追踪", async () => {
    const fetch = auditFetch()
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AuditView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("审计日志")
    expect(wrapper.find(".audit-filter-bar").exists()).toBe(true)
    expect(wrapper.text()).toContain("不可变账本")
    expect(wrapper.text()).toContain("PII 边界")
    expect(wrapper.text()).toContain("config_update")
    expect(wrapper.find('input[placeholder="开始时间"]').exists()).toBe(true)
    expect(wrapper.find("[data-testid='audit-action']").exists()).toBe(true)
    expect(
      fetch.mock.calls.some((call) => String(call[0]).includes("/admin/audit-logs/actions")),
    ).toBe(true)

    await wrapper.get("[data-testid='audit-actor']").setValue("admin01")
    await wrapper.get("[data-testid='audit-object-id']").setValue("vendor_qps")
    await wrapper.get("[data-testid='audit-more-filters']").trigger("click")
    await flushPromises()
    const correlationInput = document.querySelector<HTMLInputElement>("[data-testid='audit-correlation-id']")
    expect(correlationInput).toBeTruthy()
    correlationInput!.value = "30000000-0000-4000-8000-000000000009"
    correlationInput!.dispatchEvent(new Event("input"))
    await flushPromises()
    await wrapper.findAll("button").find((button) => button.text().includes("查询"))!.trigger("click")
    await flushPromises()
    expect(lastAuditQuery(fetch)).toContain("actor=admin01")
    expect(lastAuditQuery(fetch)).toContain("object_id=vendor_qps")
    expect(lastAuditQuery(fetch)).toContain("correlation_id=30000000-0000-4000-8000-000000000009")

    await wrapper.get("[data-testid='audit-reset']").trigger("click")
    await flushPromises()
    expect(lastAuditQuery(fetch)).not.toContain("actor=admin01")
    expect(lastAuditQuery(fetch)).not.toContain("object_id")

    await wrapper.findAll("button").find((button) => button.text().includes("详情"))!.trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("变更")
    expect(document.body.textContent).toContain("删除")
    expect(document.body.textContent).toContain("新增")
    expect(document.body.textContent).toContain("载荷受数据库 PII 约束保护")

    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window, "isSecureContext", { value: true, configurable: true })
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
    document.querySelector<HTMLElement>("[data-testid='audit-copy-correlation']")!.click()
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith("30000000-0000-4000-8000-000000000009")

    document.querySelector<HTMLElement>("[data-testid='audit-trace-correlation']")!.click()
    await flushPromises()
    expect(lastAuditQuery(fetch)).toContain("correlation_id=30000000-0000-4000-8000-000000000009")
    expect(lastAuditQuery(fetch)).not.toContain("object_id")

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("审计空态区分暂无事件与筛选无结果", async () => {
    const fetch = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/admin/audit-logs/actions")) return Promise.resolve(response([]))
      return Promise.resolve(response({ items: [], total: 0, page: 1, page_size: 20 }))
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(AuditView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("暂无审计事件")

    await wrapper.get("[data-testid='audit-actor']").setValue("admin01")
    await wrapper.findAll("button").find((button) => button.text().includes("查询"))!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("没有符合条件的审计事件")

    await wrapper.get("[data-testid='audit-clear-filters']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("暂无审计事件")
    expect(lastAuditQuery(fetch)).not.toContain("actor=")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
