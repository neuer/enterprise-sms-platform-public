import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"

import BatchDetailDrawer from "../src/components/BatchDetailDrawer.vue"
import type { BatchItem } from "../src/api/queries"

const emoji = "😀".repeat(69)

function batch(content: string): BatchItem {
  return {
    batch_no: "BATCH-1",
    category: "notice",
    channel: "web",
    app_name: null,
    creator: "operator-a",
    dept: "平台部",
    content,
    status: "pending_approval",
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
    scheduled_at: null,
    created_at: "2026-10-27T08:00:00+08:00",
  }
}

describe("详情字数按码点计数", () => {
  it("69 个 emoji 显示 69 字，计费条仍用服务端字段", async () => {
    const wrapper = mount(BatchDetailDrawer, {
      props: {
        modelValue: true,
        detailStatus: "",
        detailPage: 1,
        selected: batch(emoji),
        details: [],
        detailTotal: 0,
        detailsLoading: false,
        canWrite: false,
        canDecrypt: false,
        canScheduleOps: false,
        canResendFailed: false,
        detailUnconfirmed: false,
        cancelling: false,
        resending: false,
      },
      global: { plugins: [ElementPlus] },
      attachTo: document.body,
    })
    await flushPromises()
    const text = document.body.textContent ?? ""
    expect(text).toContain("内容长度 69 字")
    expect(text).toContain("单条 1 计费条")
    expect(text).not.toContain("138 字")
    wrapper.unmount()
  })
})
