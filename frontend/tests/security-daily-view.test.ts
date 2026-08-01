import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

const api = vi.hoisted(() => ({
  generateSecurityDailyReport: vi.fn(),
  getSecurityDailyConfiguration: vi.fn(),
  getSecurityDailyOverview: vi.fn(),
  listSecurityDailyReports: vi.fn(),
  getSecurityDailyReport: vi.fn(),
  previewSecurityDailyReport: vi.fn(),
  sendSecurityDailyReport: vi.fn(),
  retrySecurityDailyReport: vi.fn(),
  updateSecurityDailyConfiguration: vi.fn(),
}))

vi.mock("../src/api/securityDaily", () => api)

import SecurityDailyView from "../src/views/SecurityDailyView.vue"

const payload = {
  schema_version: 1,
  report_date: "2026-07-15",
  period_start: "2026-07-15T00:00:00+08:00",
  period_end: "2026-07-15T23:59:59+08:00",
  generated_at: "2026-07-16T08:00:00+08:00",
  status: "attention",
  summary: "存在一项需要人工确认的管理来源。",
  pending_confirmation: "确认该操作是否属于授权运维窗口。",
  metrics: Array.from({ length: 5 }, (_, index) => ({
    label: `指标${index + 1}`,
    value: String(index + 1),
    tone: index === 3 ? "warn" : "good",
    note: "来自已覆盖证据",
  })),
  ssh: [{ label: "认证面", value: "仅允许受控账号", assessment: "符合基线", tone: "good" }],
  web: [{ label: "5xx", value: "数据不可用", assessment: "数据不可用", tone: "warn" }],
  audit: [{ time: "14:42", actor: "security-admin", source_ip: "203.0.113.18", action: "手动运维", assessment: "待确认", tone: "warn" }],
  runtime: [{ label: "关键服务", value: "数据不可用", assessment: "数据不可用", tone: "warn" }],
  actions: [{ priority: "medium", title: "确认管理操作来源", detail: "核对授权运维窗口。" }],
  coverage: [{ source: "SSH journal", window: "00:00 — 23:59（UTC+8）", status: "完整", note: "覆盖认证证据", tone: "good" }],
}

const reports = [
  {
    id: 1, report_date: "2026-07-13", period_start: "2026-07-13T00:00:00+08:00", period_end: "2026-07-13T23:59:59+08:00",
    status: "normal", generation_status: "ready", delivery_status: "not_sent", generated_at: "2026-07-14T08:00:00+08:00", delivered_at: null,
    recipient_count: 1, retry_count: 0, last_error: null, last_error_at: null, updated_at: "2026-07-14T08:00:00+08:00", payload, timeline: [],
  },
  {
    id: 2, report_date: "2026-07-14", period_start: "2026-07-14T00:00:00+08:00", period_end: "2026-07-14T23:59:59+08:00",
    status: "attention", generation_status: "ready", delivery_status: "sent", generated_at: "2026-07-15T08:00:00+08:00", delivered_at: "2026-07-15T08:02:00+08:00",
    recipient_count: 1, retry_count: 0, last_error: null, last_error_at: null, updated_at: "2026-07-15T08:02:00+08:00", payload, timeline: [],
  },
  {
    id: 3, report_date: "2026-07-15", period_start: "2026-07-15T00:00:00+08:00", period_end: "2026-07-15T23:59:59+08:00",
    status: "high", generation_status: "ready", delivery_status: "failed", generated_at: "2026-07-16T08:00:00+08:00", delivered_at: null,
    recipient_count: 1, retry_count: 1, last_error: "mailer 暂不可用", last_error_at: "2026-07-16T08:03:00+08:00", updated_at: "2026-07-16T08:03:00+08:00", payload, timeline: [],
  },
  {
    id: 4, report_date: "2026-07-16", period_start: "2026-07-16T00:00:00+08:00", period_end: "2026-07-16T23:59:59+08:00",
    status: "normal", generation_status: "failed", delivery_status: "not_sent", generated_at: null, delivered_at: null,
    recipient_count: 0, retry_count: 0, last_error: "数据源不可用", last_error_at: null, updated_at: "2026-07-17T08:00:00+08:00", payload: null, timeline: [],
  },
  {
    id: 5, report_date: "2026-07-17", period_start: "2026-07-17T00:00:00+08:00", period_end: "2026-07-17T23:59:59+08:00",
    status: "high", generation_status: "unavailable", delivery_status: "pending", generated_at: null, delivered_at: null,
    recipient_count: 0, retry_count: 0, last_error: null, last_error_at: null, updated_at: "2026-07-18T08:00:00+08:00", payload: null, timeline: [],
  },
]

const overview = {
  enabled: true,
  configuration_state: "ready",
  schedule_time: "08:00",
  timezone: "Asia/Shanghai",
  period_description: "汇总前一上海自然日",
  last_generated_at: "2026-07-16T08:00:00+08:00",
  last_delivered_at: "2026-07-15T08:02:00+08:00",
  next_scheduled_at: "2026-07-19T08:00:00+08:00",
  latest_failure: "mailer 暂不可用",
  delivery_status: "failed",
  recipient_count: 1,
  resend_configured: true,
  sender_domain: "reports.neuer.cn",
  sender_address: "security-daily@reports.neuer.cn",
  beat_restart_required: true,
}

describe("安全日报页面", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.generateSecurityDailyReport.mockResolvedValue(reports[2])
    api.getSecurityDailyOverview.mockResolvedValue(overview)
    api.getSecurityDailyConfiguration.mockResolvedValue({
      enabled: true,
      recipients: ["security-owner@example.com"],
      resend_api_key_configured: true,
      sender_domain: "reports.neuer.cn",
      sender_address: "security-daily@reports.neuer.cn",
    })
    api.listSecurityDailyReports.mockResolvedValue({ items: reports, total: reports.length, page: 1, page_size: 20 })
    api.getSecurityDailyReport.mockImplementation((reportDate: string) => Promise.resolve(reports.find((item) => item.report_date === reportDate)))
    api.previewSecurityDailyReport.mockResolvedValue({ report_date: "2026-07-15", status: "attention", available: true, message: null, html: "", text: "安全日报预览", payload })
    api.sendSecurityDailyReport.mockResolvedValue({ request_id: "c0a80101-0000-4000-8000-000000000001", report_date: "2026-07-13", action: "send", state: "pending", idempotent: false })
    api.retrySecurityDailyReport.mockResolvedValue({ request_id: "c0a80101-0000-4000-8000-000000000002", report_date: "2026-07-15", action: "retry", state: "pending", idempotent: false })
    api.updateSecurityDailyConfiguration.mockResolvedValue({
      enabled: true,
      recipients: ["security-owner@example.com"],
      resend_api_key_configured: true,
      sender_domain: "reports.neuer.cn",
      sender_address: "security-daily@reports.neuer.cn",
    })
  })

  it("展示 normal、attention、high 及生成/投递失败和数据不可用状态", async () => {
    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("正常")
    expect(wrapper.text()).toContain("关注")
    expect(wrapper.text()).toContain("高风险")
    expect(wrapper.text()).toContain("生成失败")
    expect(wrapper.text()).toContain("投递失败")
    expect(wrapper.text()).toContain("数据不可用")
    expect(wrapper.text()).toContain("1 人（只展示数量）")
    expect(wrapper.text()).not.toContain("Resend Key")
    expect(wrapper.text()).not.toContain("security-owner@example.com")
    wrapper.unmount()
  })

  it("详情缺少报告数据时明确显示不可用且不显示伪造零值", async () => {
    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    const buttons = wrapper.findAll("button").filter((button) => button.text().includes("查看详情"))
    await buttons.at(-1)!.trigger("click")
    await flushPromises()

    expect(wrapper.text()).toContain("当前没有可展示的脱敏结构化报告")
    expect(wrapper.text()).not.toContain("攻击尝试 0")
    wrapper.unmount()
  })

  it("通过配置邮件入口读取并保存启停、Key 和收件人", async () => {
    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    await wrapper.findAll("button").find((button) => button.text().includes("配置邮件"))!.trigger("click")
    await flushPromises()

    expect(api.getSecurityDailyConfiguration).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain("当前状态：已配置")
    await wrapper.find("input[type='password']").setValue("re_ui_test")
    await wrapper.find("textarea").setValue("ops@example.com\nsecurity@example.com")
    await wrapper.findAll("button").find((button) => button.text() === "保存")!.trigger("click")
    await flushPromises()

    expect(api.updateSecurityDailyConfiguration).toHaveBeenCalledWith({
      enabled: true,
      recipients: ["ops@example.com", "security@example.com"],
      resend_api_key: "re_ui_test",
    })
    wrapper.unmount()
  })

  it("手动投递前要求确认，并只提交结构化确认动作", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    await wrapper.findAll("button").filter((button) => button.text().includes("查看详情")).at(0)!.trigger("click")
    await flushPromises()
    await wrapper.findAll("button").find((button) => button.text().includes("手动投递"))!.trigger("click")
    await flushPromises()

    expect(api.sendSecurityDailyReport).toHaveBeenCalledWith("2026-07-13")
    expect(api.sendSecurityDailyReport.mock.calls[0]).toHaveLength(1)
    wrapper.unmount()
    vi.restoreAllMocks()
  })

  it("允许管理员立即生成上一日报并在生成后打开详情", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await wrapper.findAll("button").find((button) => button.text().includes("立即生成"))!.trigger("click")
    await flushPromises()

    expect(api.generateSecurityDailyReport).toHaveBeenCalledOnce()
    expect(api.getSecurityDailyReport).toHaveBeenCalledWith("2026-07-15")
    expect(wrapper.text()).toContain("安全预览")
    wrapper.unmount()
    vi.restoreAllMocks()
  })

  it("未启用时显示配置引导且不伪造下一次运行时间", async () => {
    api.getSecurityDailyOverview.mockResolvedValue({
      ...overview,
      enabled: false,
      configuration_state: "disabled",
      next_scheduled_at: null,
      resend_configured: false,
      recipient_count: 0,
    })
    api.listSecurityDailyReports.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })

    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("日报未启用")
    expect(wrapper.text()).toContain("不会创建下一次运行计划")
    expect(wrapper.text()).not.toContain("下次")
    expect(wrapper.text()).not.toContain("D030")
    wrapper.unmount()
  })

  it("尚未生成日报时区分空状态并说明首次预览时机", async () => {
    api.getSecurityDailyOverview.mockResolvedValue({
      ...overview,
      last_generated_at: null,
      last_delivered_at: null,
      latest_failure: null,
      delivery_status: null,
      next_scheduled_at: "2026-07-19T08:00:00+08:00",
    })
    api.listSecurityDailyReports.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })

    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("尚未生成")
    expect(wrapper.text()).toContain("暂无已生成安全日报")
    expect(wrapper.text()).toContain("立即生成")
    expect(wrapper.text()).toContain("安全预览和手动投递")
    wrapper.unmount()
  })

  it("配置不完整时不开放手动投递并区分投递器与收件人状态", async () => {
    api.getSecurityDailyOverview.mockResolvedValue({
      ...overview,
      enabled: true,
      configuration_state: "dispatcher_missing",
      resend_configured: false,
      recipient_count: 0,
    })

    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("投递器未配置")
    expect(wrapper.text()).toContain("当前不会正常投递")
    expect(wrapper.findAll("button").some((button) => button.text().includes("手动投递"))).toBe(false)
    wrapper.unmount()
  })

  it("后端 503 显示错误码并清空失败列表，避免拼接部分旧状态", async () => {
    api.getSecurityDailyOverview.mockResolvedValue(overview)
    api.listSecurityDailyReports.mockRejectedValue(
      Object.assign(new Error("安全日报独立投递控制面不可用"), {
        code: "SECURITY_DAILY_UNAVAILABLE",
        status: 503,
      }),
    )

    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("安全日报独立投递控制面不可用")
    expect(wrapper.text()).toContain("安全日报记录暂不可用，请刷新重试")
    expect(wrapper.text()).not.toContain("正常")
    wrapper.unmount()
  })

  it("概览字段不完整时显示不可用而不拼装默认计划", async () => {
    api.getSecurityDailyOverview.mockResolvedValue({
      ...overview,
      configuration_state: undefined,
      next_scheduled_at: undefined,
    })
    api.listSecurityDailyReports.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })

    const wrapper = mount(SecurityDailyView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("配置状态不可用")
    expect(wrapper.text()).not.toContain("下次")
    wrapper.unmount()
  })
})
