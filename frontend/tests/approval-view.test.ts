import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import type { ApprovalCounts, ApprovalListItem } from "../src/api/approvals"
import { useApprovalBadgeStore } from "../src/stores/approvalBadge"
import { useSessionStore } from "../src/stores/session"
import ApprovalView from "../src/views/ApprovalView.vue"

const DEFAULT_COUNTS: ApprovalCounts = {
  pending: 1,
  approved: 0,
  rejected: 0,
  expired: 0,
  pending_urgent: 0,
}

let itemSeq = 0

function makeItem(overrides: Partial<ApprovalListItem> = {}): ApprovalListItem {
  itemSeq += 1
  return {
    id: itemSeq,
    batch_no: `WB2026081900${itemSeq}`,
    category: "notice",
    applicant: "operator01",
    dept: "业务一部",
    total: 120,
    segments: 1,
    estimated_segments: 120,
    scheduled_at: null,
    trigger_threshold: 100,
    trigger_threshold_source: "snapshot",
    status: "pending",
    approver: null,
    reason: null,
    expires_at: new Date(Date.now() + 5 * 3600_000).toISOString(),
    decided_at: null,
    created_at: "2026-08-19T09:12:04+08:00",
    batch_status: "pending_approval",
    deferred_reason: null,
    ...overrides,
  }
}

function listBody(
  items: ApprovalListItem[],
  counts: ApprovalCounts = DEFAULT_COUNTS,
): { total: number; counts: ApprovalCounts; items: ApprovalListItem[] } {
  return { total: items.length, counts, items }
}

type MockRoute = "list" | "detail" | "decision"

function routeOf(url: string): MockRoute {
  if (url.includes("/decision")) return "decision"
  if (/^\/api\/v1\/web\/approvals\/\d+$/.test(url)) return "detail"
  return "list"
}

interface MockResponses {
  list?: unknown
  detail?: unknown
  decision?: unknown
  decisionStatus?: number
  decisionBody?: unknown
}

function stubApprovalsFetch(responses: MockResponses) {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    const route = routeOf(url)
    if (route === "decision" && responses.decisionStatus && responses.decisionStatus !== 200) {
      return {
        ok: false,
        status: responses.decisionStatus,
        headers: { get: () => null },
        json: async () => responses.decisionBody,
      }
    }
    const body =
      route === "list" ? responses.list : route === "detail" ? responses.detail : responses.decision
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => body,
    }
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

function listCalls(fetchMock: ReturnType<typeof stubApprovalsFetch>): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => routeOf(url) === "list")
}

// 已挂载实例登记：afterEach 统一卸载，确保轮询/倒计时 interval 不泄漏到后续用例
// （真实 30s 轮询若在全量慢跑时跨用例触发，会打到当前用例的全局 fetch stub 上）
const mountedWrappers: Array<{ unmount(): void }> = []

function mountApproverView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().apply("jwt", {
    account_id: 3,
    identity_id: 13,
    provider_code: "local",
    username: "approver01",
    display_name: "开发审批员",
    dept: "业务一部",
    role: "approver",
  })
  const wrapper = mount(ApprovalView, { global: { plugins: [pinia, ElementPlus] } })
  mountedWrappers.push(wrapper)
  return wrapper
}

afterEach(() => {
  for (const wrapper of mountedWrappers.splice(0)) {
    try {
      wrapper.unmount()
    } catch {
      // 用例内已自行卸载（如轮询用例）——重复卸载忽略
    }
  }
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ""
})

describe("审批中心", () => {
  it("渲染四态计数徽章、临期角标与头部 pill，并同步导航角标", async () => {
    const counts: ApprovalCounts = { pending: 12, approved: 46, rejected: 7, expired: 3, pending_urgent: 3 }
    const fetchMock = stubApprovalsFetch({
      list: { total: 12, counts, items: [makeItem()] },
    })

    const wrapper = mountApproverView()
    await flushPromises()

    const segmented = wrapper.get("[data-testid='approval-status-seg']")
    for (const label of ["待审批", "已通过", "已驳回", "已过期"]) {
      expect(segmented.text()).toContain(label)
    }
    for (const count of ["12", "46", "7", "3"]) {
      expect(segmented.text()).toContain(count)
    }
    const pill = wrapper.get("[data-testid='approval-counts-pill']")
    expect(pill.text()).toContain("12")
    expect(pill.text()).toContain("3 临期")
    expect(wrapper.text()).toContain("当前身份 · 审批人")
    expect(wrapper.text()).toContain("30s 自动")
    expect(wrapper.find(".approval-filter-bar").exists()).toBe(true)
    expect(wrapper.find(".filter-toolbar").exists()).toBe(false)
    expect(wrapper.findComponent({ name: "ElSegmented" }).exists()).toBe(false)

    expect(useApprovalBadgeStore().pending).toBe(12)
    expect(listCalls(fetchMock)[0]).toContain("status=pending")
  })

  it("筛选与排序变更回退到第一页并透传查询参数", async () => {
    const fetchMock = stubApprovalsFetch({ list: listBody([makeItem()]) })

    const wrapper = mountApproverView()
    await flushPromises()

    const initial = listCalls(fetchMock).at(-1)!
    expect(initial).toContain("status=pending")
    expect(initial).toContain("page=1")
    expect(initial).toContain("size=20")
    expect(initial).toContain("sort=expires_asc")
    expect(initial).not.toContain("category=")
    expect(initial).not.toContain("dept=")
    expect(initial).not.toContain("q=")

    const selects = wrapper.findAllComponents({ name: "ElSelect" })
    selects[0].vm.$emit("update:modelValue", "market")
    selects[0].vm.$emit("change", "market")
    await flushPromises()
    expect(listCalls(fetchMock).at(-1)).toContain("category=market")

    const deptInput = wrapper.get("[data-testid='approval-dept-filter']")
    await deptInput.setValue("财务部")
    await deptInput.trigger("change")
    await flushPromises()
    expect(listCalls(fetchMock).at(-1)).toContain(`dept=${encodeURIComponent("财务部")}`)

    const qInput = wrapper.get("[data-testid='approval-q-filter']")
    await qInput.setValue("zhangw")
    await qInput.trigger("change")
    await flushPromises()
    expect(listCalls(fetchMock).at(-1)).toContain("q=zhangw")

    selects[1].vm.$emit("update:modelValue", "created_desc")
    selects[1].vm.$emit("change", "created_desc")
    await flushPromises()
    expect(listCalls(fetchMock).at(-1)).toContain("sort=created_desc")

    await wrapper.get("[data-testid='approval-status-approved']").trigger("click")
    await flushPromises()
    const decided = listCalls(fetchMock).at(-1)!
    expect(decided).toContain("status=approved")
    expect(decided).toContain("sort=decided_desc")
    expect(decided).toContain("page=1")
  })

  it("待审队列按剩余有效期标注临期级别并展示 HH:MM:SS 倒计时", async () => {
    const urgent = makeItem({ expires_at: new Date(Date.now() + 90 * 60_000).toISOString() })
    const soon = makeItem({ expires_at: new Date(Date.now() + 4 * 3600_000).toISOString() })
    const relaxed = makeItem({ expires_at: new Date(Date.now() + 20 * 3600_000).toISOString() })
    stubApprovalsFetch({
      list: listBody([urgent, soon, relaxed], { ...DEFAULT_COUNTS, pending: 3, pending_urgent: 1 }),
    })

    const wrapper = mountApproverView()
    await flushPromises()

    expect(wrapper.get(`[data-testid='approval-row-${urgent.id}']`).classes()).toContain("is-urgent")
    expect(wrapper.get(`[data-testid='approval-row-${soon.id}']`).classes()).toContain("is-soon")
    const relaxedRow = wrapper.get(`[data-testid='approval-row-${relaxed.id}']`)
    expect(relaxedRow.classes()).not.toContain("is-urgent")
    expect(relaxedRow.classes()).not.toContain("is-soon")

    const countdown = wrapper.get(`[data-testid='approval-row-${urgent.id}'] .approval-cd b`)
    expect(countdown.text()).toMatch(/^\d{2}:\d{2}:\d{2}$/)
    expect(wrapper.get(`[data-testid='approval-row-${urgent.id}'] .approval-cd span`).text()).toBe("后过期")
    expect(wrapper.text()).toContain("通知 ≥ 100 个号码")
  })

  it("本人提交的待审批单显示回避提示且无操作按钮", async () => {
    const mine = makeItem({ applicant: "approver01" })
    const others = makeItem({ category: "market", trigger_threshold: 50 })
    stubApprovalsFetch({ list: listBody([mine, others], { ...DEFAULT_COUNTS, pending: 2 }) })

    const wrapper = mountApproverView()
    await flushPromises()

    expect(wrapper.get(`[data-testid='approval-avoid-${mine.id}']`).text()).toBe("本人提交 · 按规则回避")
    expect(wrapper.find(`[data-testid='approval-actions-${mine.id}']`).exists()).toBe(false)
    expect(wrapper.find(`[data-testid='approval-actions-${others.id}']`).exists()).toBe(true)
    expect(wrapper.text()).toContain("营销 ≥ 50 个号码")
  })

  it("行内快捷通过携带意见提交、回显真实去向并刷新列表", async () => {
    const item = makeItem()
    const fetchMock = stubApprovalsFetch({
      list: listBody([item]),
      decision: { status: "approved", batch_status: "queued", deferred_reason: null },
    })

    const wrapper = mountApproverView()
    await flushPromises()
    const callsBefore = fetchMock.mock.calls.length

    await wrapper.get(`[data-testid='approval-quick-approve-${item.id}']`).trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='approval-quick-reason-approve']").setValue("同意发送")
    await wrapper.get("[data-testid='approval-quick-confirm-approve']").trigger("click")
    await flushPromises()

    const decisionCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes("/decision"),
    )
    expect(decisionCall).toBeDefined()
    expect(String(decisionCall![0])).toBe(`/api/v1/web/approvals/${item.id}/decision`)
    expect(decisionCall![1]?.method).toBe("POST")
    expect(JSON.parse(String(decisionCall![1]?.body))).toEqual({
      action: "approve",
      reason: "同意发送",
    })

    expect(document.body.textContent).toContain("审批已通过")
    expect(document.body.textContent).toContain("批次已进入发送队列（realtime）")
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(listCalls(fetchMock).length).toBeGreaterThanOrEqual(2)
  })

  it("行内快捷驳回原因为空时禁止确认", async () => {
    const item = makeItem()
    const fetchMock = stubApprovalsFetch({
      list: listBody([item]),
      decision: { status: "rejected", batch_status: "rejected", deferred_reason: null },
    })

    const wrapper = mountApproverView()
    await flushPromises()

    await wrapper.get(`[data-testid='approval-quick-reject-${item.id}']`).trigger("click")
    await flushPromises()
    const confirm = wrapper.get("[data-testid='approval-quick-confirm-reject']")
    expect(confirm.attributes("disabled")).toBeDefined()

    await wrapper.get("[data-testid='approval-quick-reason-reject']").setValue("含未备案链接")
    expect(wrapper.get("[data-testid='approval-quick-confirm-reject']").attributes("disabled")).toBeUndefined()
    await wrapper.get("[data-testid='approval-quick-confirm-reject']").trigger("click")
    await flushPromises()

    const decisionCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes("/decision"),
    )
    expect(JSON.parse(String(decisionCall![1]?.body))).toEqual({
      action: "reject",
      reason: "含未备案链接",
    })
    expect(document.body.textContent).toContain("审批已驳回")
  })

  it("抽屉按需加载正文并内联决策，窗外营销改派定时回显去向", async () => {
    const item = makeItem({ category: "market", total: 3200, estimated_segments: 3840 })
    const fetchMock = stubApprovalsFetch({
      list: listBody([item]),
      detail: { ...item, content: "【企业短信】会员日活动，回T退订" },
      decision: { status: "approved", batch_status: "scheduled", deferred_reason: "market_window" },
    })

    const wrapper = mountApproverView()
    await flushPromises()

    const drawer = wrapper.getComponent({ name: "ElDrawer" })
    expect(drawer.props("modelValue")).toBe(false)
    await wrapper.get(`[data-testid='approval-detail-${item.id}']`).trigger("click")
    await flushPromises()

    expect(drawer.props("modelValue")).toBe(true)
    expect(fetchMock.mock.calls.some((call) => String(call[0]) === `/api/v1/web/approvals/${item.id}`)).toBe(true)
    expect(wrapper.get("[data-testid='approval-detail-content']").text()).toContain("会员日活动，回T退订")
    expect(wrapper.text()).toContain("按需解密 · 本次查看已写敏感读审计")
    expect(wrapper.text()).toContain("待审内容（OTP 已等长打码）")
    expect(wrapper.text()).toContain("营销 / bulk")
    expect(wrapper.text()).toContain("通过后 → queued / bulk")
    expect(wrapper.text()).toContain("过期自动作废并释放配额")
    expect(wrapper.text()).not.toContain("已剔除")
    expect(drawer.props("size")).toBe("min(560px, 92vw)")

    const rejectButton = wrapper.get("[data-testid='drawer-reject']")
    expect(rejectButton.attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='drawer-decision-reason']").setValue("同意，注意营销时间窗")
    await wrapper.get("[data-testid='drawer-approve']").trigger("click")
    await flushPromises()

    const decisionCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes("/decision"),
    )
    expect(JSON.parse(String(decisionCall![1]?.body))).toEqual({
      action: "approve",
      reason: "同意，注意营销时间窗",
    })
    expect(document.body.textContent).toContain("批次已改派为定时发送")
    expect(drawer.props("modelValue")).toBe(false)
  })

  it("决策返回 409 时提示已被处理、关闭抽屉并刷新列表", async () => {
    const item = makeItem()
    const fetchMock = stubApprovalsFetch({
      list: listBody([item]),
      detail: { ...item, content: "【企业短信】内容" },
      decisionStatus: 409,
      decisionBody: { code: "STATE_CONFLICT", message: "该审批单已被处理", detail: null },
    })

    const wrapper = mountApproverView()
    await flushPromises()

    await wrapper.get(`[data-testid='approval-detail-${item.id}']`).trigger("click")
    await flushPromises()
    const listCallsBefore = listCalls(fetchMock).length

    await wrapper.get("[data-testid='drawer-approve']").trigger("click")
    await flushPromises()

    expect(document.body.textContent).toContain("该审批单已被处理或状态已变化，列表已刷新")
    expect(wrapper.getComponent({ name: "ElDrawer" }).props("modelValue")).toBe(false)
    expect(listCalls(fetchMock).length).toBe(listCallsBefore + 1)
  })

  it("已办页签展示去向列：入队、改派、释放与系统自动过期", async () => {
    const queued = makeItem({
      status: "approved",
      approver: "approver02",
      decided_at: "2026-08-19T10:00:00+08:00",
      batch_status: "queued",
    })
    const deferred = makeItem({
      category: "market",
      status: "approved",
      approver: "approver02",
      decided_at: "2026-08-19T10:05:00+08:00",
      batch_status: "scheduled",
      scheduled_at: "2026-08-19T20:00:00+08:00",
      deferred_reason: "market_window",
    })
    const rejected = makeItem({
      status: "rejected",
      approver: "approver02",
      reason: "含未备案链接域名",
      decided_at: "2026-08-19T10:10:00+08:00",
      batch_status: "rejected",
    })
    const expired = makeItem({
      status: "expired",
      decided_at: "2026-08-19T11:00:00+08:00",
      batch_status: "expired",
    })
    stubApprovalsFetch({
      list: listBody([queued, deferred, rejected, expired], {
        pending: 0,
        approved: 2,
        rejected: 1,
        expired: 1,
        pending_urgent: 0,
      }),
    })

    const wrapper = mountApproverView()
    await flushPromises()
    await wrapper.get("[data-testid='approval-status-approved']").trigger("click")
    await flushPromises()

    expect(wrapper.get(".approval-table").text()).toContain("审批人 / 决策时间")
    expect(wrapper.text()).toContain("已进入发送队列")
    expect(wrapper.text()).toContain("窗外改派为定时")
    expect(wrapper.text()).toContain("配额已释放")
    expect(wrapper.text()).toContain("原因：含未备案链接域名")
    expect(wrapper.text()).toContain("超时未决作废")
    expect(wrapper.text()).toContain("系统自动")

    await wrapper.get(`[data-testid='approval-detail-${queued.id}']`).trigger("click")
    await flushPromises()
    expect(wrapper.getComponent({ name: "ElDrawer" }).props("modelValue")).toBe(true)
  })

  it("每 30 秒静默轮询当前视图且不打断倒计时", async () => {
    vi.useFakeTimers()
    const item = makeItem()
    const fetchMock = stubApprovalsFetch({ list: listBody([item]) })

    const wrapper = mountApproverView()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(0)
    expect(listCalls(fetchMock).length).toBe(1)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(listCalls(fetchMock).length).toBe(2)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(listCalls(fetchMock).length).toBe(3)

    wrapper.unmount()
    const callsAfterUnmount = listCalls(fetchMock).length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(listCalls(fetchMock).length).toBe(callsAfterUnmount)
  })

  it("存在待审批倒计时行时秒级 tick 驱动剩余时间逐秒更新", async () => {
    vi.useFakeTimers()
    const item = makeItem({ expires_at: new Date(Date.now() + 65_000).toISOString() })
    stubApprovalsFetch({ list: listBody([item]) })

    const wrapper = mountApproverView()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(0)

    const countdown = () => wrapper.get(`[data-testid='approval-row-${item.id}'] .approval-cd b`).text()
    expect(countdown()).toBe("00:01:05")
    await vi.advanceTimersByTimeAsync(1_000)
    expect(countdown()).toBe("00:01:04")
    await vi.advanceTimersByTimeAsync(1_000)
    expect(countdown()).toBe("00:01:03")
    wrapper.unmount()
  })
})
