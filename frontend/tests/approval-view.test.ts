import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import { useSessionStore } from "../src/stores/session"
import ApprovalView from "../src/views/ApprovalView.vue"

function mountApproverView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().apply("jwt", "refresh.jwt", {
    account_id: 3,
    identity_id: 13,
    provider_code: "local",
    username: "approver01",
    display_name: "开发审批员",
    dept: "业务一部",
    role: "approver",
  })
  return mount(ApprovalView, { global: { plugins: [pinia, ElementPlus] } })
}

describe("审批中心", () => {
  it("本人提交的待审批单不显示操作按钮", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => ({
          total: 2,
          items: [
            {
              id: 1,
              batch_no: "batch-own",
              category: "notice",
              applicant: "approver01",
              dept: "业务一部",
              total: 10,
              segments: 1,
              estimated_segments: 10,
              scheduled_at: null,
              trigger_threshold: null,
              trigger_threshold_source: "legacy_unknown",
              content: "本人通知",
              status: "pending",
              approver: null,
              reason: null,
              expires_at: new Date(Date.now() + 5 * 3600_000).toISOString(),
              decided_at: null,
              created_at: "2026-07-11T08:00:00+08:00",
            },
            {
              id: 2,
              batch_no: "batch-other",
              category: "market",
              applicant: "operator01",
              dept: "业务一部",
              total: 60,
              segments: 2,
              estimated_segments: 120,
              scheduled_at: "2026-07-12T08:00:00+08:00",
              trigger_threshold: 50,
              trigger_threshold_source: "snapshot",
              content: "活动回T退订",
              status: "pending",
              approver: null,
              reason: null,
              expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
              decided_at: null,
              created_at: "2026-07-11T09:00:00+08:00",
            },
          ],
        }),
      }),
    )

    const wrapper = mountApproverView()
    await flushPromises()

    expect(wrapper.get(".approval-toolbar").classes()).toContain("filter-toolbar")
    expect(wrapper.find("[data-testid='approval-actions-1']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='approval-actions-2']").text()).toContain("通过")
    expect(wrapper.find(".approval-mobile-list").exists()).toBe(false)
    expect(wrapper.text()).toContain("本人提交")
    expect(wrapper.text()).toContain("业务一部")
    expect(wrapper.text()).toContain("60")
    expect(wrapper.text()).toContain("120")
    expect(wrapper.text()).toContain("营销 ≥ 50 个号码")
    expect(wrapper.text()).toContain("历史阈值不可确认")
    expect(wrapper.text()).toContain("2026-07-12")
    expect(wrapper.find(".category-tag--market").exists()).toBe(true)

    const relaxedExpiry = wrapper.get("[data-testid='approval-expiry-1'] .approval-expiry")
    expect(relaxedExpiry.text()).toContain("剩余")
    expect(relaxedExpiry.text()).toContain("小时")
    expect(relaxedExpiry.classes()).not.toContain("urgent")
    const urgentExpiry = wrapper.get("[data-testid='approval-expiry-2'] .approval-expiry")
    expect(urgentExpiry.text()).toContain("剩余")
    expect(urgentExpiry.text()).toContain("分钟")
    expect(urgentExpiry.classes()).toContain("urgent")

    const drawer = wrapper.getComponent({ name: "ElDrawer" })
    expect(drawer.props("modelValue")).toBe(false)
    const detailTrigger = wrapper.get("[data-testid='approval-detail-1']")
    expect(detailTrigger.attributes("aria-label")).toContain("batch-own")
    await detailTrigger.trigger("click")
    expect(drawer.props("modelValue")).toBe(true)
    expect(wrapper.get("[data-testid='drawer-approval-expiry']").text()).toContain("剩余")
    vi.unstubAllGlobals()
  })

  it("连接升级前后端时对缺失审批事实显式降级", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => ({
          total: 1,
          items: [{
            id: 9,
            batch_no: "batch-legacy",
            category: "notice",
            applicant: "operator01",
            dept: "业务一部",
            total: 120,
            content: "升级前审批记录",
            status: "pending",
            approver: null,
            reason: null,
            created_at: "2026-07-11T08:00:00+08:00",
          }],
        }),
      }),
    )

    const wrapper = mountApproverView()
    await flushPromises()

    expect(wrapper.text()).toContain("预计计费")
    expect(wrapper.text()).toContain("历史阈值不可确认")
    expect(wrapper.text()).toContain("计划暂不可用")
    expect(wrapper.text()).toContain("有效期暂不可用")
    expect(wrapper.find(".approval-item").exists()).toBe(true)
    vi.unstubAllGlobals()
  })

  it("已办列表展示审批人、决策时间与系统自动过期", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => ({
          total: 2,
          items: [
            {
              id: 7,
              batch_no: "batch-approved",
              category: "notice",
              applicant: "operator01",
              dept: "业务一部",
              total: 30,
              segments: 1,
              estimated_segments: 30,
              scheduled_at: null,
              trigger_threshold: 100,
              trigger_threshold_source: "snapshot",
              content: "验证码通知",
              status: "approved",
              approver: "approver02",
              reason: "同意发送",
              expires_at: "2026-07-12T08:00:00+08:00",
              decided_at: "2026-07-12T01:00:00+08:00",
              created_at: "2026-07-11T08:00:00+08:00",
            },
            {
              id: 8,
              batch_no: "batch-expired",
              category: "market",
              applicant: "operator02",
              dept: "业务二部",
              total: 80,
              segments: 2,
              estimated_segments: 160,
              scheduled_at: null,
              trigger_threshold: 50,
              trigger_threshold_source: "snapshot",
              content: "活动回T退订",
              status: "expired",
              approver: null,
              reason: null,
              expires_at: "2026-07-12T08:00:00+08:00",
              decided_at: "2026-07-12T08:00:00+08:00",
              created_at: "2026-07-11T08:00:00+08:00",
            },
          ],
        }),
      }),
    )

    const wrapper = mountApproverView()
    await flushPromises()
    wrapper.getComponent({ name: "ElSegmented" }).vm.$emit("change", "approved")
    await flushPromises()

    expect(wrapper.text()).toContain("审批人 / 决策时间")
    expect(wrapper.text()).toContain("approver02")
    expect(wrapper.text()).toContain("系统自动")
    expect(wrapper.text()).toContain("2026-07-12 01:00:00")
    expect(wrapper.text()).toContain("2026-07-12 08:00:00")
    expect(wrapper.find("[data-testid='approval-expiry-7']").exists()).toBe(false)

    const detailTrigger = wrapper.get("[data-testid='approval-detail-7']")
    await detailTrigger.trigger("click")
    expect(wrapper.text()).toContain("审批人")
    expect(wrapper.text()).toContain("决策时间")
    expect(wrapper.text()).toContain("审批意见")
    expect(wrapper.text()).toContain("同意发送")
    vi.unstubAllGlobals()
  })
})
