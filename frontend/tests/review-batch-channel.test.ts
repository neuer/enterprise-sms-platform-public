import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { describe, expect, it, vi } from "vitest"
import BatchView from "../src/views/BatchView.vue"
import { useSessionStore } from "../src/stores/session"
vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }))

describe("review: 批次渠道授权边界", () => {
  it("Web 会话不能对 API 批次提供可点击的重发操作", async () => {
    const batch = {
      batch_no: "SYNTHETIC-API-BATCH",
      category: "notice",
      channel: "api",
      app_name: "合成应用",
      creator: null,
      dept: "合成测试部门",
      content: "合成审查内容",
      status: "completed",
      deferred_reason: null,
      resend_of: null,
      is_test: false,
      segments: 1,
      quota_cost: 1,
      total: 1,
      removed_freq_limit: 0,
      pending: 0,
      sent: 0,
      delivered: 0,
      failed: 1,
      unknown: 0,
      other: 0,
      scheduled_at: null,
      created_at: "2026-09-12T14:00:00+08:00",
    }
    const fetch = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () =>
        url.includes("/details?")
          ? { total: 0, items: [] }
          : url.includes("/batches/SYNTHETIC-API-BATCH")
            ? batch
            : url.includes("/admin/apps")
              ? []
              : { total: 1, items: [batch] },
    }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "admin"
    const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
    try {
      await flushPromises()
      const detail = wrapper.findAll("button").find((button) => button.text().includes("查看详情"))
      await detail!.trigger("click")
      await flushPromises()
      const resend = wrapper.find("[data-testid='resend-failed']")
      expect(
        resend.exists() && resend.attributes("disabled") === undefined,
        "API 批次的 Bearer 重发必被后端渠道校验拒绝",
      ).toBe(false)
    } finally {
      wrapper.unmount()
      vi.unstubAllGlobals()
    }
  })
})
