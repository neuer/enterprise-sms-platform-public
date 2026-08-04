import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
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

  it("状态筛选包含草稿选项", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [{ ...template, vendor_state: "draft" }],
    }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get(".template-toolbar").text()).toContain("草稿")
    expect(wrapper.get(".template-toolbar").text()).toContain("待审核")
    vi.unstubAllGlobals()
  })

  it("已通过模板操作列显示占位且无可用写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null },
      json: async () => [{ ...template, id: 3, vendor_state: "approved" }],
    }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='template-sync-3']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-edit-3']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-delete-3']").exists()).toBe(false)
    const actionCell = wrapper.get(".template-table .el-table__row td:last-child")
    expect(actionCell.text()).toBe("—")
    expect(actionCell.get(".muted").attributes("title")).toContain("已通过审核")
    vi.unstubAllGlobals()
  })

  it("部门为空时列表与移动卡片均显示占位", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null },
      json: async () => [{ ...template, id: 4, dept: "" }],
    }))
    const pinia = applyRole("admin")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    const deptCell = wrapper.get(".template-table .el-table__row td:nth-child(4)")
    expect(deptCell.text()).toBe("—")
    expect(deptCell.get("span").classes()).toContain("muted")
    expect(wrapper.get(".template-mobile-list dl > div:last-child dd").text()).toBe("—")
    vi.unstubAllGlobals()
  })

  it("编辑器内联提示占位与变量声明不一致，并可从内容识别变量", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [template],
    }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-template']").trigger("click")
    await flushPromises()
    const textarea = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    expect(textarea).toBeTruthy()
    textarea.value = "尊敬的{1}，验证码{2}"
    textarea.dispatchEvent(new Event("input"))
    // el-form-item 的错误状态经 100ms 防抖后才渲染
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).toContain("未声明变量最大长度")

    ;(document.querySelector("[data-testid='template-sync-vars']") as HTMLElement).click()
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).not.toContain("未声明变量最大长度")
    expect(document.querySelectorAll(".variable-row")).toHaveLength(2)

    // 改为跳号占位后提示必须从 {1} 连续编号
    textarea.value = "验证码{2}"
    textarea.dispatchEvent(new Event("input"))
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).toContain("必须从 {1} 开始连续编号")
    vi.unstubAllGlobals()
  })

  it("校验不通过时不发起提交请求", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [template],
    })
    vi.stubGlobal("fetch", fetchMock)
    const warning = vi.spyOn(ElMessage, "warning").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-template']").trigger("click")
    await flushPromises()
    ;(document.querySelector("[data-testid='template-submit']") as HTMLElement).click()
    await flushPromises()

    expect(warning).toHaveBeenCalledWith("请填写模板名称")
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/templates")).length).toBe(1)
    vi.unstubAllGlobals()
  })

  it("取消删除确认框不发起请求也不报错", async () => {
    const rejectedTemplate = { ...template, id: 2, name: "通知", vendor_state: "rejected" }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => [rejectedTemplate],
    })
    vi.stubGlobal("fetch", fetchMock)
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const error = vi.spyOn(ElMessage, "error")
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='template-delete-2']").trigger("click")
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect(
      fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "DELETE").length,
    ).toBe(0)
    vi.unstubAllGlobals()
  })

  it("同步失败时给出错误反馈", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/sync")) {
        return {
          ok: false, status: 502,
          headers: { get: () => null },
          json: async () => ({ code: "VENDOR_ERROR", message: "厂商模板接口不可用", detail: null }),
        }
      }
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => [template] }
    })
    vi.stubGlobal("fetch", fetchMock)
    const error = vi.spyOn(ElMessage, "error").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='template-sync-1']").trigger("click")
    await flushPromises()
    expect(error).toHaveBeenCalledWith("厂商模板接口不可用")
    vi.unstubAllGlobals()
  })
})
