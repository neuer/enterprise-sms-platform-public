import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import SignView from "../src/views/SignView.vue"
import { useSessionStore } from "../src/stores/session"

const pendingSign = {
  id: 1,
  name: "待审签名",
  vendor_sign_id: "20",
  vendor_state: "pending",
  vendor_reject_reason: null,
}
const approvedSign = {
  id: 2,
  name: "青鸾平台",
  vendor_sign_id: "21",
  vendor_state: "approved",
  vendor_reject_reason: null,
}

function applyRole(role: "admin" | "approver" | "operator"): ReturnType<typeof createPinia> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().apply("jwt", {
    account_id: 1,
    identity_id: 11,
    provider_code: "local",
    username: `${role}01`, display_name: "测试用户", dept: "平台部", role,
  })
  return pinia
}

function okList(payload: unknown) {
  return { ok: true, status: 200, headers: { get: () => null }, json: async () => payload }
}

describe("签名管理", () => {
  it("展示规范签名、厂商编号和审核状态", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okList([pendingSign, approvedSign])))
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

  it("非管理员看不到申请与行内写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okList([pendingSign])))
    const pinia = applyRole("operator")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='new-sign']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-sync-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-delete-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='mobile-sign-sync-1']").exists()).toBe(false)
    expect(wrapper.text()).toContain("【待审签名】")
    vi.unstubAllGlobals()
  })

  it("已通过签名操作列显示占位且无可用写操作", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okList([approvedSign])))
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='sign-sync-2']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-edit-2']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-delete-2']").exists()).toBe(false)
    const actionCell = wrapper.get(".sign-table .el-table__row td:last-child")
    expect(actionCell.text()).toBe("—")
    expect(actionCell.get(".muted").attributes("title")).toContain("已通过审核")
    const mobileFooter = wrapper.get(".sign-mobile-list article footer")
    expect(mobileFooter.text()).toBe("—")
    vi.unstubAllGlobals()
  })

  it("编辑器内联提示方括号并在修正后清除", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okList([pendingSign])))
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-sign']").trigger("click")
    await flushPromises()
    const input = document.querySelector(".el-drawer input") as HTMLInputElement
    expect(input).toBeTruthy()
    input.value = "青鸾【平台"
    input.dispatchEvent(new Event("input"))
    // el-form-item 的错误状态经 100ms 防抖后才渲染
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).toContain("签名名称不得包含方括号")

    input.value = "青鸾平台"
    input.dispatchEvent(new Event("input"))
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).not.toContain("签名名称不得包含方括号")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("名称为空或含方括号时不发起提交请求", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okList([pendingSign]))
    vi.stubGlobal("fetch", fetchMock)
    const warning = vi.spyOn(ElMessage, "warning").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-sign']").trigger("click")
    await flushPromises()
    ;(document.querySelector("[data-testid='sign-submit']") as HTMLElement).click()
    await flushPromises()
    expect(warning).toHaveBeenCalledWith("请填写签名名称")

    const input = document.querySelector(".el-drawer input") as HTMLInputElement
    input.value = "青鸾】平台"
    input.dispatchEvent(new Event("input"))
    await flushPromises()
    ;(document.querySelector("[data-testid='sign-submit']") as HTMLElement).click()
    await flushPromises()
    expect(warning).toHaveBeenCalledWith("签名名称不得包含方括号")

    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST").length).toBe(0)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("取消删除确认框不发起请求也不报错", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okList([pendingSign]))
    vi.stubGlobal("fetch", fetchMock)
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const error = vi.spyOn(ElMessage, "error")
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-delete-1']").trigger("click")
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect(
      fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "DELETE").length,
    ).toBe(0)
    vi.unstubAllGlobals()
  })

  it("同步失败时给出错误反馈且按钮恢复可用", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/sync")) {
        return {
          ok: false, status: 502,
          headers: { get: () => null },
          json: async () => ({ code: "VENDOR_ERROR", message: "厂商签名接口不可用", detail: null }),
        }
      }
      return okList([pendingSign])
    })
    vi.stubGlobal("fetch", fetchMock)
    const error = vi.spyOn(ElMessage, "error").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-sync-1']").trigger("click")
    await flushPromises()
    expect(error).toHaveBeenCalledWith("厂商签名接口不可用")
    expect(wrapper.get("[data-testid='sign-sync-1']").attributes("aria-disabled")).not.toBe("true")
    vi.unstubAllGlobals()
  })
})
