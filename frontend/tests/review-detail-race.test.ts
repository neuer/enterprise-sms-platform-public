import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { describe, expect, it, vi } from "vitest"
const daily = vi.hoisted(() => ({
  getSecurityDailyOverview: vi.fn().mockResolvedValue(null),
  listSecurityDailyReports: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
  getSecurityDailyReport: vi.fn(),
  generateSecurityDailyReport: vi.fn(),
  previewSecurityDailyReport: vi.fn(),
  retrySecurityDailyReport: vi.fn(),
  sendSecurityDailyReport: vi.fn(),
}))
const ops = vi.hoisted(() => ({ listRawLogs: vi.fn(), replayRaw: vi.fn() }))
const confirm = vi.hoisted(() => vi.fn())
vi.mock("../src/lib/confirm", () => ({
  useConfirmActions: () => ({
    confirmAuditedAction: async (options: { isCurrent?: () => boolean }) =>
      (await confirm()) && (options.isCurrent?.() ?? true),
  }),
}))
vi.mock("../src/api/securityDaily", () => daily)
vi.mock("../src/api/ops", () => ops)
vi.mock("../src/components/SecurityDailyConfigDialog.vue", () => ({ default: { template: "<div />" } }))
import SecurityDailyView from "../src/views/SecurityDailyView.vue"
import OpsRawTab from "../src/views/ops/OpsRawTab.vue"

describe("review: 管理与运维边界", () => {
  it("关闭旧日报并打开新日报后，旧响应不得覆盖新详情", async () => {
    let resolveOld!: (value: unknown) => void
    const report = (id: number) => ({
      id,
      report_date: "2026-09-11",
      period_start: "2026-09-11T00:00:00+08:00",
      period_end: "2026-09-12T00:00:00+08:00",
      status: "normal",
      generation_source: "manual",
      generation_status: "ready",
      delivery_status: "not_sent",
      generated_at: null,
      delivered_at: null,
      recipient_count: 0,
      retry_count: 0,
      last_error: null,
      last_error_at: null,
      updated_at: "2026-09-12T14:00:00+08:00",
      payload: null,
      timeline: [],
    })
    daily.getSecurityDailyReport
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve
        }),
      )
      .mockResolvedValueOnce(report(202))
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() })
    const wrapper = mount(SecurityDailyView, { global: { plugins: [ElementPlus] } })
    try {
      const vm = wrapper.vm as unknown as {
        openReport: (id: number) => Promise<void>
        drawerOpen: boolean
        selected: { id: number }
      }
      const old = vm.openReport(201)
      vm.drawerOpen = false
      await vm.openReport(202)
      expect(vm.selected.id).toBe(202)
      resolveOld(report(201))
      await old
      expect(vm.selected.id, "旧日报覆盖当前查看对象，后续预览/投递将取错 ID").toBe(202)
    } finally {
      wrapper.unmount()
    }
  })
  it("预览乱序及确认期间切换详情不会改变投递目标", async () => {
    const report = (id: number) => ({
      id,
      report_date: "2026-09-11",
      payload: null,
      timeline: [],
      generation_status: "ready",
      delivery_status: "not_sent",
    })
    daily.getSecurityDailyReport.mockImplementation(async (id: number) => report(id))
    let finishPreview!: (value: unknown) => void
    let finishConfirm!: (value: boolean) => void
    daily.previewSecurityDailyReport.mockReturnValueOnce(
      new Promise((resolve) => {
        finishPreview = resolve
      }),
    )
    confirm.mockReturnValueOnce(
      new Promise((resolve) => {
        finishConfirm = resolve
      }),
    )
    const wrapper = mount(SecurityDailyView, { global: { plugins: [ElementPlus] } })
    const vm = wrapper.vm as unknown as {
      openReport: (id: number) => Promise<void>
      openPreview: () => Promise<void>
      requestDelivery: (action: "send") => Promise<void>
      selected: { id: number }
      previewOpen: boolean
      previewText: string
    }
    try {
      await vm.openReport(201)
      const preview = vm.openPreview()
      const delivery = vm.requestDelivery("send")
      await vm.openReport(202)
      finishPreview({ available: true, text: "old preview" })
      await preview
      expect(vm.previewOpen).toBe(false)
      expect(vm.previewText).toBe("")
      finishConfirm(true)
      await delivery
      expect(daily.sendSecurityDailyReport).not.toHaveBeenCalled()
      expect(vm.selected.id).toBe(202)
    } finally {
      wrapper.unmount()
    }
  })
  it.each([
    ["complete", "unattempted", "automatic", true],
    ["complete_too_large", "transient_failure", "manual", true],
    ["complete", "protocol_invalid", "never", false],
    ["truncated", "unattempted", "automatic", false],
    ["protocol_invalid", "unattempted", "manual", false],
    ["unknown_legacy", "unattempted", "manual", false],
    ["complete", "processed", "automatic", false],
    ["future", "unattempted", "automatic", false],
    ["complete", "future", "automatic", false],
    ["complete", "unattempted", "future", false],
  ])("RAW 资格 %s/%s/%s 的双布局展示", async (capture, parse, eligibility, allowed) => {
    ops.listRawLogs.mockResolvedValue({
      items: [
        {
          id: 901,
          source: "report",
          item_count: 0,
          custom_id_count: 0,
          processed: false,
          error: null,
          fetched_at: "2026-09-12T14:00:00+08:00",
          capture_state: capture,
          parse_state: parse,
          replay_eligibility: eligibility,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    })
    const wrapper = mount(OpsRawTab, { props: { active: true }, global: { plugins: [ElementPlus] } })
    try {
      await flushPromises()
      const enabledReplay = wrapper
        .findAll("button")
        .filter((button) => button.text() === "重放" && button.attributes("disabled") === undefined)
      expect(enabledReplay.length).toBe(allowed ? 2 : 0)
    } finally {
      wrapper.unmount()
    }
  })
})
