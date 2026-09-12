import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { SendResult, WebMessagePayload } from "../src/api/webMessages"

const api = vi.hoisted(() => ({
  sendWebMessage: vi.fn(),
  previewBilling: vi.fn().mockResolvedValue(null),
  uploadPhones: vi.fn(),
  downloadImportInvalidFile: vi.fn(),
}))
vi.mock("../src/api/webMessages", () => api)
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock("../src/api/templates", () => ({ listTemplates: vi.fn().mockResolvedValue([]) }))
vi.mock("../src/api/signs", () => ({ listSigns: vi.fn().mockResolvedValue([]) }))
vi.mock("../src/api/dashboard", () => ({
  getDashboard: vi.fn().mockResolvedValue({ ui_policy: { test_send_max: 5 } }),
}))
import SendView from "../src/views/SendView.vue"

type Editor = {
  form: { content: string; mobilesText: string; category: string; remark: string }
  pastedMobiles: string[]
  busy: boolean
  sendResult: SendResult | null
  submit: () => Promise<void>
  chooseCategory: (value: string) => void
  resetForAnother: () => void
}
function result(batch = "synthetic-batch"): SendResult {
  return {
    batch_no: batch,
    status: "queued",
    accepted: 1,
    removed_duplicate: 0,
    removed_blacklist: 0,
    removed_freq_limit: 0,
    quota_cost: 1,
  } as SendResult
}
async function setup() {
  const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as Editor
  vm.form.content = "合成通知"
  vm.form.mobilesText = "199" + "0".repeat(7) + "1"
  await wrapper.vm.$nextTick()
  return { wrapper, vm }
}
beforeEach(() => {
  api.sendWebMessage.mockReset()
})

describe("提交快照与草稿生命周期", () => {
  it("在途锁定全部输入、分段选择和重置，响应附带原提交摘要", async () => {
    let resolve!: (value: SendResult) => void
    api.sendWebMessage.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const { wrapper, vm } = await setup()
    try {
      const sending = vm.submit()
      await wrapper.vm.$nextTick()
      for (const control of wrapper.findAll<HTMLInputElement>("input, textarea, select")) {
        expect(control.element.disabled).toBe(true)
      }
      for (const button of wrapper.findAll<HTMLButtonElement>(".category-switch button, .filter-seg button")) {
        expect(button.element.disabled).toBe(true)
      }
      vm.chooseCategory("market")
      vm.resetForAnother()
      await vm.submit()
      expect(vm.form.category).toBe("notice")
      expect(vm.form.content).toBe("合成通知")
      expect(api.sendWebMessage).toHaveBeenCalledTimes(1)
      const payload = api.sendWebMessage.mock.calls[0][0] as WebMessagePayload
      vm.pastedMobiles.push("199" + "0".repeat(7) + "2")
      expect(payload.mobiles).toHaveLength(1)
      resolve(result())
      await sending
      await wrapper.vm.$nextTick()
      expect(wrapper.get('[data-testid="submitted-summary"]').text()).toContain("通知短信 · 手工粘贴")
    } finally {
      wrapper.unmount()
    }
  })

  it.each(["content", "remark"] as const)("草稿 %s 被程序变更后旧响应不得回填", async (field) => {
    let resolve!: (value: SendResult) => void
    api.sendWebMessage.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const { wrapper, vm } = await setup()
    try {
      const sending = vm.submit()
      vm.form[field] = "新草稿"
      resolve(result())
      await sending
      expect(vm.sendResult).toBeNull()
      expect(vm.busy).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it("失败重试复用原键，修改草稿后才换键", async () => {
    api.sendWebMessage.mockRejectedValue(new Error("synthetic uncertain response"))
    const { wrapper, vm } = await setup()
    try {
      await vm.submit()
      await vm.submit()
      const calls = api.sendWebMessage.mock.calls
      expect(calls[0][0]).toEqual(calls[1][0])
      vm.form.content = "新合成通知"
      await vm.submit()
      expect(calls[2][0].biz_id).not.toBe(calls[1][0].biz_id)
    } finally {
      wrapper.unmount()
    }
  })

  it.each(["resolve", "reject"])("清除会话后旧 %s 不能恢复结果或解除新请求锁", async (ending) => {
    let resolve!: (value: SendResult) => void
    let reject!: (value: Error) => void
    api.sendWebMessage.mockImplementationOnce(
      () =>
        new Promise((done, fail) => {
          resolve = done
          reject = fail
        }),
    )
    const { wrapper, vm } = await setup()
    try {
      const first = vm.submit()
      window.dispatchEvent(new Event("session-clearing"))
      expect(vm.form.content).toBe("")
      expect(vm.form.mobilesText).toBe("")
      vm.form.content = "新会话合成通知"
      vm.form.mobilesText = "199" + "0".repeat(7) + "2"
      let finish!: (value: SendResult) => void
      api.sendWebMessage.mockImplementationOnce(
        () =>
          new Promise((done) => {
            finish = done
          }),
      )
      const second = vm.submit()
      if (ending === "resolve") resolve(result("old"))
      else reject(new Error("old failure"))
      await first
      expect(vm.busy).toBe(true)
      expect(vm.sendResult).toBeNull()
      finish(result("new"))
      await second
      expect(vm.sendResult?.batch_no).toBe("new")
    } finally {
      wrapper.unmount()
    }
  })
})
