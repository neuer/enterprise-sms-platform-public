import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import { useSessionStore } from "../src/stores/session"
import TemplateView from "../src/views/TemplateView.vue"

const template = {
  id: 1,
  name: "验证码",
  content: "验证码{1}",
  var_specs: [{ pos: 1, max_len: 6 }],
  dept: "平台部",
  vendor_template_id: "21",
  vendor_state: "pending",
  vendor_reject_reason: null,
}

function applyRole(role: "admin" | "approver" | "operator"): ReturnType<typeof createPinia> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().apply("jwt", "refresh.jwt", {
    account_id: 1,
    identity_id: 11,
    provider_code: "local",
    username: `${role}01`, display_name: "测试用户", dept: "平台部", role,
  })
  return pinia
}

describe("模板管理", () => {
  it("展示平台占位、变量长度和厂商审核状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: { get: () => null },
        json: async () => [template],
      }),
    )
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get(".template-toolbar").classes()).toContain("filter-toolbar")
    expect(wrapper.text()).toContain("验证码{1}")
    expect(wrapper.text()).toContain("待审核")
    expect(wrapper.text()).toContain("最大 6 字")
    expect(wrapper.find(".status-tag--pending").exists()).toBe(true)
    expect(wrapper.find(".template-table").exists()).toBe(true)
    expect(wrapper.find(".template-mobile-list").exists()).toBe(true)
    expect(wrapper.get("[data-testid='template-mobile-detail-1']").attributes("aria-label")).toContain("验证码")

    const detailDrawer = wrapper.findAllComponents({ name: "ElDrawer" })[0]
    expect(detailDrawer.props("modelValue")).toBe(false)
    const detailTrigger = wrapper.get("[data-testid='template-detail-1']")
    expect(detailTrigger.attributes("aria-label")).toContain("验证码")
    await detailTrigger.trigger("keydown", { key: " " })
    expect(detailDrawer.props("modelValue")).toBe(true)
    vi.unstubAllGlobals()
  })

  it("审批员只能查看模板且不会看到任何写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [template],
    }))
    const pinia = applyRole("approver")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='new-template']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-sync-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-edit-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-delete-1']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='template-detail-1']").text()).toContain("验证码")
    vi.unstubAllGlobals()
  })

  it("操作员可看到模板写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [template],
    }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='new-template']").text()).toContain("新建模板")
    expect(wrapper.get("[data-testid='template-sync-1']").text()).toContain("同步")
    vi.unstubAllGlobals()
  })
})
