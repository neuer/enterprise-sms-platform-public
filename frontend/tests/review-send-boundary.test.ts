import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { describe, expect, it, vi } from "vitest"

const api = vi.hoisted(() => ({
  sendWebMessage: vi.fn().mockRejectedValue(new Error("synthetic request stopped")),
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

describe("review: 定时发送安全边界", () => {
  it.each(["", "invalid date"])("定时缺少有效时间 %s 时不得发出立即发送请求", async (scheduledAt) => {
    api.sendWebMessage.mockClear()
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    try {
      await flushPromises()
      const vm = wrapper.vm as unknown as {
        form: { mobilesText: string; content: string; scheduleEnabled: boolean; scheduledAt: string }
        submit: () => Promise<void>
      }
      vm.form.mobilesText = "199" + "0".repeat(7) + "1"
      vm.form.content = "隔离审查合成通知"
      vm.form.scheduleEnabled = true
      vm.form.scheduledAt = scheduledAt
      await wrapper.vm.$nextTick()
      await vm.submit()
      const sentWithoutSchedule = api.sendWebMessage.mock.calls.some(([payload]) => payload.scheduled_at === undefined)
      expect(sentWithoutSchedule, "未填时间却发送了不含 scheduled_at 的 POST").toBe(false)
      vm.form.scheduledAt = "2099-09-12T14:00:00+08:00"
      await vm.submit()
      expect(api.sendWebMessage.mock.calls.at(-1)?.[0].scheduled_at).toBe("2099-09-12T06:00:00.000Z")
      api.sendWebMessage.mockClear()
      vm.form.scheduledAt = ""
      await vm.submit()
      expect(api.sendWebMessage).not.toHaveBeenCalled()
      vm.form.scheduleEnabled = false
      await vm.submit()
      expect(api.sendWebMessage.mock.calls.at(-1)?.[0].scheduled_at).toBeUndefined()
      expect(api.sendWebMessage).toHaveBeenCalledTimes(1)
    } finally {
      wrapper.unmount()
    }
  })
})
