import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import ReplyView from "../src/views/ReplyView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => name === "content-length" && status === 200 && body === undefined ? "0" : null },
    json: async () => body,
  }
}

describe("回复查询", () => {
  it("展示掩码回复并允许操作员将回复号码加入退订黑名单", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({
        total: 1,
        items: [{
          id: 5,
          phone: "138****8000",
          content: "TD",
          batch_no: "BATCH-1",
          reply_time: "2026-07-12T08:00:00+08:00",
        }],
      }))
      .mockResolvedValueOnce(response(undefined))
      .mockResolvedValueOnce(response({ total: 1, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.get("form.reply-filter").classes()).toContain("filter-grid")
    expect(wrapper.text()).toContain("上行回复")
    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.text()).toContain("TD")
    const optout = wrapper.findAll("button").find((button) => button.text().includes("退订加黑"))
    expect(optout).toBeTruthy()
    await optout!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/replies/5/blacklist")
    expect(fetch.mock.calls[1][1].method).toBe("POST")
    vi.unstubAllGlobals()
  })

  it("只读角色不显示退订写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ total: 0, items: [] })))
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"
    const wrapper = mount(ReplyView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain("退订加黑")
    expect(wrapper.text()).toContain("尚未采集到上行回复")
    vi.unstubAllGlobals()
  })
})
