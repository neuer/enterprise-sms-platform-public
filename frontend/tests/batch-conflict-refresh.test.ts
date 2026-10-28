import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"

import BatchView from "../src/views/BatchView.vue"
import { useSessionStore } from "../src/stores/session"

vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }))

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

const scheduled = {
  batch_no: "BATCH-SCHEDULED",
  category: "notice",
  channel: "web",
  app_name: null,
  creator: "operator01",
  dept: "合成部门",
  content: "合成内容",
  status: "scheduled",
  deferred_reason: null,
  resend_of: null,
  is_test: false,
  segments: 1,
  quota_cost: 1,
  total: 1,
  removed_freq_limit: 0,
  pending: 1,
  sent: 0,
  delivered: 0,
  failed: 0,
  unknown: 0,
  other: 0,
  scheduled_at: "2026-10-24T09:00:00+08:00",
  created_at: "2026-10-23T09:00:00+08:00",
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it("改期得到 409 后重读详情，完成态不再保留改期入口，且没有第二次写请求", async () => {
  const completed = { ...scheduled, status: "completed", pending: 0, delivered: 1 }
  const posts: string[] = []
  let detailReads = 0
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === "POST" && target.includes("/reschedule")) {
      posts.push(target)
      return response({ code: "STATE_CONFLICT", message: "状态已变化" }, 409)
    }
    if (target.includes("/details?")) return response({ total: 0, items: [] })
    if (target.endsWith("/batches/BATCH-SCHEDULED")) {
      detailReads += 1
      return response(detailReads === 1 ? scheduled : completed)
    }
    if (target.includes("/batches?")) {
      const row = posts.length ? completed : scheduled
      return response({ total: 1, items: [row], status_counts: {} })
    }
    return response({ total: 0, items: [] })
  })
  vi.stubGlobal("fetch", fetch)
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "operator"
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  try {
    await flushPromises()
    const open = wrapper.findAll("button").find((button) => button.text().includes("查看详情"))
    await open!.trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='reschedule-batch']").trigger("click")
    await flushPromises()
    const confirm = wrapper.findAll("button").find((button) => button.text().includes("确认改期"))
    await confirm!.trigger("click")
    await flushPromises()
    expect(posts).toHaveLength(1)
    expect(wrapper.find("[data-testid='reschedule-batch']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='cancel-batch']").exists()).toBe(false)
    expect(wrapper.text()).toContain("已完成")
  } finally {
    wrapper.unmount()
  }
})

it("详情读取 404 后暂停操作并提供只读重试，不自动再次改期", async () => {
  const posts: string[] = []
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === "POST" && target.includes("/reschedule")) {
      posts.push(target)
      return response({ code: "STATE_CONFLICT", message: "状态已变化" }, 409)
    }
    if (target.includes("/details?")) return response({ total: 0, items: [] })
    if (target.endsWith("/batches/BATCH-SCHEDULED") && posts.length) {
      return response({ code: "NOT_FOUND", message: "不存在" }, 404)
    }
    if (target.endsWith("/batches/BATCH-SCHEDULED")) return response(scheduled)
    if (target.includes("/batches?")) return response({ total: 1, items: [scheduled], status_counts: {} })
    return response({ total: 0, items: [] })
  })
  vi.stubGlobal("fetch", fetch)
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "operator"
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  try {
    await flushPromises()
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("查看详情"))!
      .trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='reschedule-batch']").trigger("click")
    await flushPromises()
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("确认改期"))!
      .trigger("click")
    await flushPromises()
    expect(posts).toHaveLength(1)
    expect(wrapper.get("[data-testid='batch-status-unconfirmed']").text()).toContain("暂时无法确认")
    expect(wrapper.find("[data-testid='reschedule-batch']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='retry-batch-detail']").exists()).toBe(true)
  } finally {
    wrapper.unmount()
  }
})

it.each(["2026-10-31T00:00:00Z", "2026-10-31T08:00:00+08:00"])(
  "改期回显把 %s 转为上海钟面，未编辑保存保持同一时刻",
  async (scheduledAt) => {
    const batch = { ...scheduled, scheduled_at: scheduledAt }
    const writes: unknown[] = []
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).endsWith("/reschedule")) {
          writes.push(JSON.parse(String(init?.body)))
          return response({})
        }
        if (String(url).includes("/details?")) return response({ total: 0, items: [] })
        if (String(url).endsWith("/batches/BATCH-SCHEDULED")) return response(batch)
        return response({ total: 1, items: [batch], status_counts: {} })
      }),
    )
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    try {
      await flushPromises()
      await wrapper
        .findAll("button")
        .find((b) => b.text().includes("查看详情"))!
        .trigger("click")
      await flushPromises()
      await wrapper.get("[data-testid='reschedule-batch']").trigger("click")
      await flushPromises()
      const picker = wrapper.findAllComponents({ name: "ElDatePicker" }).find((p) => p.props("type") === "datetime")!
      expect(picker.find("input").element.value).toBe("2026-10-31 08:00:00")
      await wrapper
        .findAll("button")
        .find((b) => b.text() === "确认改期")!
        .trigger("click")
      await flushPromises()
      expect(writes).toEqual([{ scheduled_at: "2026-10-31T08:00:00+08:00" }])
    } finally {
      wrapper.unmount()
    }
  },
)
