import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox, ElPagination, ElSelect } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import BatchView from "../src/views/BatchView.vue"
import MessageView from "../src/views/MessageView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
  }
}

describe("批次与号码查询", () => {
  it("展示批次列表并在右侧抽屉加载掩码明细", async () => {
    const batch = {
      batch_no: "BATCH-1",
      category: "notice",
      channel: "web",
      app_name: null,
      creator: "operator-a",
      dept: "平台部",
      content: "系统通知",
      status: "completed",
      deferred_reason: null,
      resend_of: null,
      is_test: false,
      segments: 1,
      quota_cost: 2,
      total: 2,
      removed_freq_limit: 0,
      delivered: 1,
      failed: 1,
      unknown: 0,
      scheduled_at: null,
      created_at: "2026-07-12T08:00:00+08:00",
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [batch] }))
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response({
        total: 1,
        items: [{
          id: 9,
          phone: "138****8000",
          status: "delivered",
          vendor_task_id: "task-1",
          report_desc: "DELIVRD",
          report_time: "2026-07-12T08:01:00+08:00",
        }],
      }))
      .mockResolvedValueOnce(response({ phone: "13800138000" }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "admin"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(wrapper.get("form.query-filter").classes()).toContain("filter-grid")
    expect(wrapper.text()).toContain("批次列表")
    expect(wrapper.text()).toContain("BATCH-1")
    expect(wrapper.find(".category-tag--notice").exists()).toBe(true)
    expect(wrapper.find(".status-tag--completed").exists()).toBe(true)
    const optionLabels = wrapper.findAllComponents({ name: "ElOption" }).map((item) => item.props("label"))
    for (const label of ["待审批", "已驳回", "定时中", "排队中", "发送中", "已完成", "已取消", "余额阻断", "已过期"]) {
      expect(optionLabels).toContain(label)
    }
    const detail = wrapper.findAll("button").find((item) => item.text().includes("查看详情"))
    await detail!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.text()).toContain("operator-a")
    expect(wrapper.text()).toContain("单条计费条")
    expect(wrapper.get("[data-testid='resend-failed']").text()).toContain("失败号码重发")
    await wrapper.get("[data-testid='batch-phone-decrypt-9']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("13800138000")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/messages/batches/BATCH-1")
    expect(fetch.mock.calls[2][0]).toBe("/api/v1/messages/batches/BATCH-1/details?page=1&size=20")
    vi.unstubAllGlobals()
  })

  it("批次号模糊查询，抽屉明细支持状态筛选与分页", async () => {
    const batch = {
      batch_no: "BATCH-9", category: "notice", channel: "api", app_name: "通知应用",
      creator: "operator-b", dept: "平台部", content: "系统通知", status: "completed",
      deferred_reason: null, resend_of: null, is_test: false, segments: 1, quota_cost: 21,
      total: 21, removed_freq_limit: 0, delivered: 20, failed: 1, unknown: 0,
      scheduled_at: null, created_at: "2026-07-12T08:00:00+08:00",
    }
    const message = {
      id: 9, phone: "138****8000", status: "delivered",
      vendor_task_id: "task-1", report_desc: "DELIVRD", report_time: "2026-07-12T08:01:00+08:00",
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [batch] }))
      .mockResolvedValueOnce(response({ total: 1, items: [batch] }))
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response({ total: 21, items: [message] }))
      .mockResolvedValueOnce(response({ total: 1, items: [{ ...message, status: "failed" }] }))
      .mockResolvedValueOnce(response({ total: 1, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "admin"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.find('input[placeholder="模糊匹配批次号"]').setValue("BATCH-9")
    await wrapper.find("form.query-filter").trigger("submit")
    await flushPromises()
    expect(fetch.mock.calls[1][0]).toContain("/api/v1/web/batches?")
    expect(fetch.mock.calls[1][0]).toContain("batch_no=BATCH-9")

    await wrapper.findAll("button").find((item) => item.text().includes("查看详情"))!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[3][0]).toBe("/api/v1/messages/batches/BATCH-9/details?page=1&size=20")
    expect(wrapper.text()).toContain("共 21 条")

    const detailSelect = wrapper
      .findAllComponents({ name: "ElSelect" })
      .find((item) => item.attributes("data-testid") === "batch-detail-status")!
    detailSelect.vm.$emit("update:modelValue", "failed")
    detailSelect.vm.$emit("change", "failed")
    await flushPromises()
    expect(fetch.mock.calls[4][0]).toBe("/api/v1/messages/batches/BATCH-9/details?page=1&size=20&status=failed")

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("抽屉明细翻页保留状态筛选", async () => {
    const batch = {
      batch_no: "BATCH-10", category: "notice", channel: "web", app_name: null,
      creator: "operator-c", dept: "平台部", content: "系统通知", status: "sending",
      deferred_reason: null, resend_of: null, is_test: false, segments: 1, quota_cost: 40,
      total: 40, removed_freq_limit: 0, delivered: 10, failed: 0, unknown: 0,
      scheduled_at: null, created_at: "2026-07-12T08:00:00+08:00",
    }
    const message = {
      id: 9, phone: "138****8000", status: "sent",
      vendor_task_id: "task-1", report_desc: null, report_time: null,
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [batch] }))
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response({ total: 40, items: [message] }))
      .mockResolvedValueOnce(response({ total: 25, items: [message] }))
      .mockResolvedValueOnce(response({ total: 25, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "admin"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll("button").find((item) => item.text().includes("查看详情"))!.trigger("click")
    await flushPromises()

    const detailSelect = wrapper
      .findAllComponents({ name: "ElSelect" })
      .find((item) => item.attributes("data-testid") === "batch-detail-status")!
    detailSelect.vm.$emit("update:modelValue", "sent")
    detailSelect.vm.$emit("change", "sent")
    await flushPromises()
    expect(fetch.mock.calls[3][0]).toBe("/api/v1/messages/batches/BATCH-10/details?page=1&size=20&status=sent")

    await wrapper.find(".batch-detail-pagination .btn-next").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[4][0]).toBe("/api/v1/messages/batches/BATCH-10/details?page=2&size=20&status=sent")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("定时批次详情提供取消和改期并调用现有后端端点", async () => {
    const batch = {
      batch_no: "BATCH-SCHEDULED", category: "market", channel: "web", app_name: null,
      creator: "operator01", dept: "业务一部", content: "营销通知", status: "scheduled",
      deferred_reason: null, resend_of: null, is_test: false, segments: 1, quota_cost: 1,
      total: 1, removed_freq_limit: 0, delivered: 0, failed: 0, unknown: 0,
      scheduled_at: "2026-07-13T08:00:00+08:00", created_at: "2026-07-12T08:00:00+08:00",
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [batch] }))
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response({ total: 0, items: [] }))
      .mockResolvedValueOnce(response(undefined))
      .mockResolvedValueOnce(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll("button").find((item) => item.text().includes("查看详情"))!.trigger("click")
    await flushPromises()

    expect(wrapper.get("[data-testid='cancel-batch']").text()).toContain("取消批次")
    expect(wrapper.get("[data-testid='reschedule-batch']").text()).toContain("改期")
    await wrapper.get("[data-testid='cancel-batch']").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[3][0]).toBe("/api/v1/messages/batches/BATCH-SCHEDULED/cancel")
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("号码列表保持掩码并允许 approver 单条授权查看", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({
        total: 1,
        items: [{
          id: 9,
          phone: "138****8000",
          status: "delivered",
          report_desc: "DELIVRD",
          report_time: "2026-07-12T08:01:00+08:00",
          created_at: "2026-07-12T08:00:00+08:00",
          batch_no: "BATCH-1",
          category: "notice",
          content: "系统通知",
          sender: "通知应用",
        }],
      }))
      .mockResolvedValueOnce(response({ phone: "13800138000" }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "approver"
    const wrapper = mount(MessageView, { global: { plugins: [pinia, ElementPlus] } })
    expect(wrapper.get("form.message-search").classes()).toContain("filter-grid")
    await wrapper.find('input[placeholder="输入 11 位手机号"]').setValue("13800138000")
    await wrapper.find("form").trigger("submit")
    await flushPromises()

    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.text()).not.toContain("13800138000")
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web/messages?phone=13800138000&page=1")
    const reveal = wrapper.findAll("button").find((item) => item.text().includes("授权查看"))
    await reveal!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("13800138000")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/messages/9/phone/decrypt")
    vi.unstubAllGlobals()
  })

  it("号码列表支持类别状态筛选、分页并展示失败回报", async () => {
    const fetch = vi.fn().mockResolvedValue(response({
      total: 45,
      items: [{
        id: 9,
        phone: "138****8000",
        status: "failed",
        report_desc: "UNDELIV",
        report_time: "2026-07-12T08:01:00+08:00",
        created_at: "2026-07-12T08:00:00+08:00",
        batch_no: "BATCH-1",
        category: "notice",
        content: "系统通知",
        sender: "通知应用",
      }],
    }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"
    const wrapper = mount(MessageView, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.find('input[placeholder="输入 11 位手机号"]').setValue("13800138000")
    await wrapper.find("form").trigger("submit")
    await flushPromises()

    expect(wrapper.text()).toContain("UNDELIV")
    expect(wrapper.find(".report-desc").exists()).toBe(true)

    const selects = wrapper.findAllComponents(ElSelect)
    selects[0].vm.$emit("update:modelValue", "notice")
    selects[0].vm.$emit("change", "notice")
    await flushPromises()
    selects[1].vm.$emit("update:modelValue", "failed")
    selects[1].vm.$emit("change", "failed")
    await flushPromises()

    expect(fetch.mock.calls[1][0]).toContain("category=notice")
    expect(fetch.mock.calls[1][0]).not.toContain("status=")
    expect(fetch.mock.calls[2][0]).toContain("category=notice")
    expect(fetch.mock.calls[2][0]).toContain("status=failed")
    expect(fetch.mock.calls[2][0]).toContain("page=1")

    const pager = wrapper.getComponent(ElPagination)
    pager.vm.$emit("update:currentPage", 2)
    pager.vm.$emit("current-change", 2)
    await flushPromises()
    expect(fetch.mock.calls[3][0]).toContain("page=2")
    expect(fetch.mock.calls[3][0]).toContain("status=failed")
    vi.unstubAllGlobals()
  })

  it("时间线展示号码徽标并将用户回复标为回声", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      badge: { blacklisted: true, blacklist_source: "reply_optout", recv_30d: 3 },
      truncated: true,
      events: [{
        ts: "2026-07-12T08:02:00+08:00",
        direction: "in",
        category: "notice",
        batch_no: "BATCH-1",
        content: "退订",
        status: null,
        sender: "用户",
      }],
    })))
    const wrapper = mount(MessageView, { global: { plugins: [createPinia(), ElementPlus] } })
    await wrapper.find('input[placeholder="输入 11 位手机号"]').setValue("13800138000")
    const timeline = wrapper.findAll("button").find((item) => item.text().includes("时间线"))
    await timeline!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("已在黑名单")
    expect(wrapper.text()).toContain("近30日 3 条")
    expect(wrapper.text()).toContain("↩ 用户回复")
    expect(wrapper.text()).toContain("仅显示最近 500 条")
    vi.unstubAllGlobals()
  })
})
