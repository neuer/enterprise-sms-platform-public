import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox, ElSelect } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import CallbackView from "../src/views/CallbackView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name === "content-length" && body === undefined ? "0" : null) },
    json: async () => body,
  }
}

const deadTask = {
  id: 9,
  event_id: "10000000-0000-4000-8000-000000000009",
  correlation_id: "30000000-0000-4000-8000-000000000009",
  app_id: 7,
  app_name: "IAM",
  event: "batch.finished",
  batch_no: "BATCH-1",
  reference_count: 0,
  status: "dead",
  retry_count: 5,
  next_retry_at: null,
  lease_id: null,
  lease_expires_at: null,
  takeover_count: 2,
  stalled: false,
  last_http_code: 500,
  last_error: "TimeoutError",
  created_at: "2026-07-12T08:00:00+08:00",
  finished_at: "2026-07-12T09:00:00+08:00",
}

const appOptions = [{ id: 7, name: "IAM" }]

function routeFetch(listBody: unknown, overrides?: (url: string, init: RequestInit) => unknown) {
  return vi.fn().mockImplementation((input: string, init: RequestInit = {}) => {
    if (overrides) {
      const overridden = overrides(input, init)
      if (overridden) return Promise.resolve(overridden)
    }
    if (input === "/api/v1/web/admin/apps") return Promise.resolve(response(appOptions))
    if (input.startsWith("/api/v1/web/admin/callbacks?")) return Promise.resolve(response(listBody))
    return Promise.resolve(response(undefined))
  })
}

/** 展开 h() VNode 确认框正文为纯文本，便于断言后果与审计细字。 */
function vnodeText(node: unknown): string {
  if (typeof node === "string") return node
  if (Array.isArray(node)) return node.map(vnodeText).join("")
  if (node && typeof node === "object" && "children" in node) {
    return vnodeText((node as { children: unknown }).children)
  }
  return ""
}

describe("回调任务", () => {
  beforeEach(() => {
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("单行检索条、常驻规则条与 dead 脉冲呈现，密集事实收进详情抽屉", async () => {
    const fetch = routeFetch({ total: 1, dead_total: 1, items: [deadTask] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.find("form.callback-filter-bar").exists()).toBe(true)
    expect(wrapper.find(".callback-filter").exists()).toBe(false)
    expect(wrapper.text()).toContain("回调任务")
    expect(wrapper.text()).toContain("dead 总计")
    expect(wrapper.text()).toContain("60s → 5m → 15m → 1h → 1h")
    expect(wrapper.text()).toContain("IAM")
    expect(wrapper.text()).toContain("终止重试")
    expect(wrapper.text()).toContain("TimeoutError")
    // 关联 RID、接管计数等密集事实收进详情抽屉，不再占据表格
    expect(wrapper.text()).not.toContain("30000000-0000-4000-8000-000000000009")
    expect(wrapper.text()).not.toContain("接管 2 次")
    expect(wrapper.text()).not.toContain("callback.internal")
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web/admin/apps")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/admin/callbacks?page=1&size=20")

    await wrapper.get("[data-testid='callback-detail-9']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("回调任务详情")
    expect(wrapper.text()).toContain("30000000-0000-4000-8000-000000000009")
    expect(wrapper.text()).toContain("接管次数")
    expect(wrapper.text()).toContain("BATCH-1")
    wrapper.unmount()
  })

  it("dead 任务手动重推先确认后果与审计说明，确认后重查列表", async () => {
    const success = vi.spyOn(ElMessage, "success")
    const fetch = routeFetch({ total: 1, dead_total: 1, items: [deadTask] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='callback-retry-9']").trigger("click")
    await flushPromises()

    const confirm = vi.mocked(ElMessageBox.confirm)
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(confirm.mock.calls[0][1]).toBe("确认手动重推")
    const body = vnodeText(confirm.mock.calls[0][0])
    expect(body).toContain("CB-9")
    expect(body).toContain("重置为待投递并清零重试计数")
    expect(body).toContain("审计日志")
    expect(confirm.mock.calls[0][2]).toMatchObject({
      confirmButtonText: "重推任务",
      customClass: "callback-confirm-box",
    })
    expect(
      fetch.mock.calls.some(([url, init]) => String(url).endsWith("/callbacks/9/retry") && init?.method === "POST"),
    ).toBe(true)
    expect(success).toHaveBeenCalledWith("回调任务已重新入队 · 本次操作已记入审计")
    expect(String(fetch.mock.calls.at(-1)![0])).toContain("/admin/callbacks?page=1")
    wrapper.unmount()
  })

  it("取消重推确认不产生写请求", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const fetch = routeFetch({ total: 1, dead_total: 1, items: [deadTask] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='callback-retry-9']").trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.filter(([url]) => String(url).includes("/retry"))).toHaveLength(0)
    wrapper.unmount()
  })

  it("重推请求在途时忽略重复点击", async () => {
    let resolveRetry!: (value: unknown) => void
    const fetch = routeFetch({ total: 1, dead_total: 1, items: [deadTask] }, (url) => {
      if (String(url).endsWith("/retry")) {
        return new Promise((resolve) => {
          resolveRetry = resolve
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const retry = wrapper.get("[data-testid='callback-retry-9']")
    await retry.trigger("click")
    await retry.trigger("click")
    const retryCalls = () => fetch.mock.calls.filter(([url]) => String(url).includes("/retry"))
    expect(retryCalls()).toHaveLength(1)

    resolveRetry(response(undefined))
    await flushPromises()
    expect(retryCalls()).toHaveLength(1)
    wrapper.unmount()
  })

  it("状态与事件 seg 点选即重查，组合筛选透传查询参数并展示下次重试时间", async () => {
    const retrying = {
      ...deadTask,
      id: 10,
      status: "retrying",
      retry_count: 2,
      next_retry_at: "2026-07-12T08:05:00+08:00",
      finished_at: null,
    }
    const fetch = routeFetch({ total: 1, dead_total: 0, items: [retrying] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("08:05:00")

    const listCalls = () =>
      fetch.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith("/api/v1/web/admin/callbacks?"))

    await wrapper.get("[data-testid='callback-status-retrying']").trigger("click")
    await flushPromises()
    expect(listCalls().at(-1)).toContain("status=retrying")
    expect(wrapper.get("[data-testid='callback-status-retrying']").classes()).toContain("on")

    await wrapper.get("[data-testid='callback-event-report']").trigger("click")
    await flushPromises()
    expect(listCalls().at(-1)).toContain("event=message.report")

    const select = wrapper.findAllComponents(ElSelect)[0]
    select.vm.$emit("update:modelValue", 7)
    select.vm.$emit("change", 7)
    await flushPromises()
    expect(listCalls().at(-1)).toContain("app_id=7")

    await wrapper.get("input[data-testid='callback-batch-filter']").setValue("BATCH-1")
    await wrapper.get("form.callback-filter-bar").trigger("submit")
    await flushPromises()
    const lastCall = listCalls().at(-1) ?? ""
    expect(lastCall).toContain("batch_no=BATCH-1")
    expect(lastCall).toContain("status=retrying")
    expect(lastCall).toContain("app_id=7")
    expect(lastCall).toContain("event=message.report")

    await wrapper.get("[data-testid='callback-reset']").trigger("click")
    await flushPromises()
    const reset = listCalls().at(-1) ?? ""
    expect(reset).not.toContain("status=")
    expect(reset).not.toContain("app_id=")
    expect(reset).not.toContain("event=")
    expect(reset).not.toContain("batch_no=")
    wrapper.unmount()
  })

  it("空态区分空库与筛选无结果并提供清除筛选入口", async () => {
    const fetch = routeFetch({ total: 0, dead_total: 0, items: [] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain("当前没有回调任务")

    await wrapper.get("input[data-testid='callback-batch-filter']").setValue("BATCH-9")
    await wrapper.get("form.callback-filter-bar").trigger("submit")
    await flushPromises()
    expect(wrapper.text()).toContain("没有符合筛选的回调任务")
    expect(wrapper.text()).not.toContain("当前没有回调任务")

    await wrapper.get("[data-testid='clear-callback-filters']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("当前没有回调任务")
    expect((wrapper.get("input[data-testid='callback-batch-filter']").element as HTMLInputElement).value).toBe("")
    wrapper.unmount()
  })

  it("嵌入运维中心时隐藏页头，dead 总计沉底栏常驻", async () => {
    const fetch = routeFetch({ total: 1, dead_total: 1, items: [deadTask] })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      props: { embedded: true },
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.find(".callback-heading").exists()).toBe(false)
    expect(wrapper.text()).not.toContain("DELIVERY TRACE")
    expect(wrapper.text()).toContain("共 1 项 · 每页 20 · dead 总计 1")
    expect(wrapper.find("form.callback-filter-bar").exists()).toBe(true)
    wrapper.unmount()
  })
})
