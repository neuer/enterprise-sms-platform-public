import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElSelect } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import CallbackView from "../src/views/CallbackView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => name === "content-length" && body === undefined ? "0" : null },
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

describe("回调任务", () => {
  it("展示安全摘要并允许 dead 任务手动重推", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(appOptions))
      .mockResolvedValueOnce(response({ total: 1, dead_total: 1, items: [deadTask] }))
      .mockResolvedValueOnce(response(undefined))
      .mockResolvedValueOnce(response({ total: 0, dead_total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.get(".callback-filter").classes()).toContain("filter-toolbar")
    expect(wrapper.text()).toContain("回调任务")
    expect(wrapper.text()).toContain("dead 总计")
    expect(wrapper.text()).toContain("IAM")
    expect(wrapper.text()).toContain("已死亡")
    expect(wrapper.text()).toContain("TimeoutError")
    expect(wrapper.text()).toContain("接管 2 次")
    expect(wrapper.text()).toContain("30000000-0000-4000-8000-000000000009")
    expect(wrapper.text()).not.toContain("callback.internal")
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web/admin/apps")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/admin/callbacks?page=1")
    const retry = wrapper.findAll("button").find((button) => button.text().includes("手动重推"))
    await retry!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[2][0]).toBe("/api/v1/web/admin/callbacks/9/retry")
    expect(fetch.mock.calls[2][1].method).toBe("POST")
    vi.unstubAllGlobals()
  })

  it("重推请求在途时忽略重复点击", async () => {
    let resolveRetry!: (value: unknown) => void
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(appOptions))
      .mockResolvedValueOnce(response({ total: 1, dead_total: 1, items: [deadTask] }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve }))
      .mockResolvedValue(response({ total: 0, dead_total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    const retry = wrapper.findAll("button").find((button) => button.text().includes("手动重推"))
    await retry!.trigger("click")
    await retry!.trigger("click")
    const retryCalls = () => fetch.mock.calls.filter(([url]) => String(url).includes("/retry"))
    expect(retryCalls()).toHaveLength(1)

    resolveRetry(response(undefined))
    await flushPromises()
    expect(retryCalls()).toHaveLength(1)
    vi.unstubAllGlobals()
  })

  it("组合筛选透传查询参数并展示下次重试时间", async () => {
    const retrying = {
      ...deadTask,
      id: 10,
      status: "retrying",
      retry_count: 2,
      next_retry_at: "2026-07-12T08:05:00+08:00",
      finished_at: null,
    }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(appOptions))
      .mockResolvedValue(response({ total: 1, dead_total: 0, items: [retrying] }))
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain("重试中")
    expect(wrapper.text()).toContain("08:05:00")

    const selects = wrapper.findAllComponents(ElSelect)
    selects[0].vm.$emit("update:modelValue", "retrying")
    selects[0].vm.$emit("change", "retrying")
    await flushPromises()
    expect(String(fetch.mock.calls[2][0])).toContain("status=retrying")

    selects[1].vm.$emit("update:modelValue", 7)
    selects[1].vm.$emit("change", 7)
    await flushPromises()
    expect(String(fetch.mock.calls[3][0])).toContain("app_id=7")

    selects[2].vm.$emit("update:modelValue", "message.report")
    selects[2].vm.$emit("change", "message.report")
    await flushPromises()
    expect(String(fetch.mock.calls[4][0])).toContain("event=message.report")

    const batchInput = wrapper.get("input#callback-batch-no")
    await batchInput.setValue("BATCH-1")
    await batchInput.trigger("change")
    await flushPromises()
    const lastCall = String(fetch.mock.calls.at(-1)![0])
    expect(lastCall).toContain("batch_no=BATCH-1")
    expect(lastCall).toContain("status=retrying")
    expect(lastCall).toContain("app_id=7")
    expect(lastCall).toContain("event=message.report")
    vi.unstubAllGlobals()
  })
})
