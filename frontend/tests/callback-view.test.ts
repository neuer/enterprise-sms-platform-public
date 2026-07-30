import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
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

describe("回调任务", () => {
  it("展示安全摘要并允许 dead 任务手动重推", async () => {
    const item = {
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
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ total: 1, items: [item] }))
      .mockResolvedValueOnce(response(undefined))
      .mockResolvedValueOnce(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(CallbackView, {
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.get(".callback-filter").classes()).toContain("filter-toolbar")
    expect(wrapper.text()).toContain("回调任务")
    expect(wrapper.text()).toContain("IAM")
    expect(wrapper.text()).toContain("已死亡")
    expect(wrapper.text()).toContain("TimeoutError")
    expect(wrapper.text()).toContain("接管 2 次")
    expect(wrapper.text()).toContain("30000000-0000-4000-8000-000000000009")
    expect(wrapper.text()).not.toContain("callback.internal")
    const retry = wrapper.findAll("button").find((button) => button.text().includes("手动重推"))
    await retry!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/admin/callbacks/9/retry")
    expect(fetch.mock.calls[1][1].method).toBe("POST")
    vi.unstubAllGlobals()
  })
})
