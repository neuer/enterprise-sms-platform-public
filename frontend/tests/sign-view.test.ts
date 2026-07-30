import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import SignView from "../src/views/SignView.vue"
import { useSessionStore } from "../src/stores/session"

describe("签名管理", () => {
  it("展示规范签名、厂商编号和审核状态", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => [
        {
          id: 1,
          name: "待审签名",
          vendor_sign_id: "20",
          vendor_state: "pending",
          vendor_reject_reason: null,
        },
        {
          id: 2,
          name: "青鸾平台",
          vendor_sign_id: "21",
          vendor_state: "approved",
          vendor_reject_reason: null,
        },
      ],
    }))
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "admin"
    const wrapper = mount(SignView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.find(".sign-mobile-list").exists()).toBe(true)
    expect(wrapper.text()).toContain("厂商意见")
    expect(wrapper.get("[data-testid='mobile-sign-sync-1']").text()).toContain("同步")
    expect(wrapper.text()).toContain("【青鸾平台】")
    expect(wrapper.text()).toContain("已通过")
    expect(wrapper.text()).toContain("21")
    vi.unstubAllGlobals()
  })
})
