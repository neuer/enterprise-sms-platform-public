import { mount } from "@vue/test-utils"
import ElementPlus, { ElDatePicker } from "element-plus"
import { createPinia } from "pinia"
import { describe, expect, it, vi } from "vitest"
vi.mock("../src/components/ReportTrendChart.vue", () => ({ default: { template: "<div />" } }))
const api = vi.hoisted(() => ({
  getReport: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1 }),
  createDetailExport: vi.fn(),
  getExportTask: vi.fn(),
  issueExportStepUp: vi.fn(),
  downloadExport: vi.fn(),
}))
vi.mock("../src/api/reports", () => api)
import ReportView from "../src/views/ReportView.vue"

describe("review: 报表日期清空", () => {
  it("真实日期控件清空后 filters 计算不得抛异常", async () => {
    const errors: unknown[] = []
    const wrapper = mount(ReportView, {
      global: { plugins: [createPinia(), ElementPlus], config: { errorHandler: (error) => errors.push(error) } },
    })
    try {
      const picker = wrapper.findComponent(ElDatePicker)
      expect(picker.props("clearable")).toBe(true)
      picker.vm.$emit("update:modelValue", null)
      const vm = wrapper.vm as unknown as { filters: unknown }
      expect(() => vm.filters).not.toThrow()
    } finally {
      wrapper.unmount()
    }
  })
})
