import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import { useSessionStore } from "../src/stores/session"
import ApprovalView from "../src/views/ApprovalView.vue"

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
              created_at: "2026-07-11T09:00:00+08:00",
            },
          ],
        }),
      }),
    )
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

    const wrapper = mount(ApprovalView, { global: { plugins: [pinia, ElementPlus] } })
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

    const drawer = wrapper.getComponent({ name: "ElDrawer" })
    expect(drawer.props("modelValue")).toBe(false)
    const detailTrigger = wrapper.get("[data-testid='approval-detail-1']")
    expect(detailTrigger.attributes("aria-label")).toContain("batch-own")
    await detailTrigger.trigger("click")
    expect(drawer.props("modelValue")).toBe(true)
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

    const wrapper = mount(ApprovalView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("预计计费")
    expect(wrapper.text()).toContain("历史阈值不可确认")
    expect(wrapper.text()).toContain("计划暂不可用")
    expect(wrapper.find(".approval-item").exists()).toBe(true)
    vi.unstubAllGlobals()
  })
})
