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
const rejectedSign = {
  id: 3,
  name: "优惠早知道",
  vendor_sign_id: null,
  vendor_state: "rejected",
  vendor_reject_reason: "签名与已有品牌近似",
}

/** ElMessageBox 确认框可传字符串或 VNode；测试只抽取可见文案。 */
function vnodeText(value: unknown): string {
  if (typeof value === "string") return value
  if (value == null) return ""
  if (typeof value === "object" && "children" in value) {
    const children = (value as { children?: unknown }).children
    if (typeof children === "string") return children
    if (Array.isArray(children)) return children.map(vnodeText).join("")
    return vnodeText(children)
  }
  return String(value)
}

function applyRole(role: "admin" | "approver" | "operator"): ReturnType<typeof createPinia> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().apply("jwt", {
    account_id: 1,
    identity_id: 11,
    provider_code: "local",
    username: `${role}01`,
    display_name: "测试用户",
    dept: "平台部",
    role,
  })
  return pinia
}

function okJson(payload: unknown) {
  return { ok: true, status: 200, headers: { get: () => null }, json: async () => payload }
}

/** fetch 路由：/admin/apps 与 /signs 分别返回；默认 signs 列表。 */
function mockFetch(signs: unknown, apps: unknown[] = []) {
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    if (String(input).includes("/admin/apps")) return okJson(apps)
    return okJson(signs)
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

describe("签名管理", () => {
  it("规范签名作主列，状态副行区分待审核双阶段与驳回原因", async () => {
    mockFetch([pendingSign, approvedSign, rejectedSign, { ...pendingSign, id: 4, vendor_sign_id: null }])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find(".sign-filter-bar").exists()).toBe(true)
    expect(wrapper.find(".filter-toolbar").exists()).toBe(false)
    expect(wrapper.get(".sign-filter-note").text()).toBe("接口全量返回 · 前端过滤")
    const table = wrapper.get(".sign-table")
    expect(table.text()).toContain("【青鸾平台】")
    expect(table.text()).toContain("平台 #2")
    // 已通过显示厂商编号；已绑定 pending 显示「厂商审核中」；未绑定显示「提交厂商中」
    expect(table.text()).toContain("厂商 #21")
    expect(table.text()).toContain("厂商审核中 · #20")
    expect(table.text()).toContain("提交厂商中…")
    // 已拒绝行内直接显示驳回原因
    expect(table.text()).toContain("签名与已有品牌近似")
    expect(wrapper.find(".status-tag--pending").exists()).toBe(true)
    expect(wrapper.find(".sign-mobile-list").exists()).toBe(true)
    expect(wrapper.get("[data-testid='mobile-sign-detail-2']").attributes("aria-label")).toContain("青鸾平台")
    vi.unstubAllGlobals()
  })

  it("仅管理员可对待审核且未绑定厂商编号的签名发起关联", async () => {
    const unboundPending = { ...pendingSign, id: 9, vendor_sign_id: null }
    mockFetch([unboundPending, pendingSign, approvedSign, rejectedSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get(".sign-table").text()).toContain("提交厂商中…")
    expect(wrapper.find("[data-testid='sign-sync-9']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-adopt-9']").exists()).toBe(true)
    expect(wrapper.find("[data-testid='mobile-sign-adopt-9']").exists()).toBe(true)
    expect(wrapper.find("[data-testid='sign-delete-9']").exists()).toBe(true)
    expect(wrapper.find("[data-testid='sign-adopt-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-adopt-2']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-adopt-3']").exists()).toBe(false)
    wrapper.unmount()

    mockFetch([unboundPending])
    const operatorPinia = applyRole("operator")
    const operator = mount(SignView, { global: { plugins: [operatorPinia, ElementPlus] } })
    await flushPromises()
    expect(operator.find("[data-testid='sign-adopt-9']").exists()).toBe(false)
    expect(operator.find("[data-testid='mobile-sign-adopt-9']").exists()).toBe(false)
    operator.unmount()
    vi.unstubAllGlobals()
  })

  it("关联已有签名固定提交当前名称与正整数厂商 ID，成功只提示任务已入队", async () => {
    const unboundPending = { ...pendingSign, id: 9, name: "厦门钨业", vendor_sign_id: null }
    const fetchMock = mockFetch([unboundPending])
    const success = vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, {
      attachTo: document.body,
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='sign-adopt-9']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("请先在厂商后台核对")
    expect(document.body.textContent).toContain("厂商签名 ID 必须对应规范签名【厦门钨业】")
    expect(document.body.textContent).toContain("不会新建厂商签名，也不代表审核已经通过")

    const input = document.querySelector("[data-testid='sign-adopt-vendor-id']") as HTMLInputElement
    input.value = "112074"
    input.dispatchEvent(new Event("input"))
    await flushPromises()
    ;(document.querySelector("[data-testid='sign-adopt-submit']") as HTMLElement).click()
    await flushPromises()

    const request = fetchMock.mock.calls.find(
      ([url, init]) => String(url).endsWith("/signs/9/adopt-existing") && (init as RequestInit)?.method === "POST",
    )
    expect(request).toBeTruthy()
    expect(JSON.parse(String((request?.[1] as RequestInit).body))).toEqual({
      vendor_sign_id: 112074,
      confirmed_name: "厦门钨业",
    })
    expect(success).toHaveBeenCalledWith("关联已有厂商签名与审核状态查询已入队")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("状态 seg 展示各状态计数并按筛选联动", async () => {
    mockFetch([pendingSign, approvedSign, rejectedSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='sign-state-all']").text()).toContain("3")
    expect(wrapper.get("[data-testid='sign-state-pending']").text()).toContain("1")
    expect(wrapper.get("[data-testid='sign-state-approved']").text()).toContain("1")
    expect(wrapper.get("[data-testid='sign-state-rejected']").text()).toContain("1")

    await wrapper.get("[data-testid='sign-state-rejected']").trigger("click")
    await flushPromises()
    expect(wrapper.findAll(".sign-table .el-table__row")).toHaveLength(1)
    expect(wrapper.get(".sign-table").text()).toContain("优惠早知道")
    expect(wrapper.get(".sign-foot").text()).toContain("共 1 个签名")
    expect(wrapper.get(".sign-foot-role").text()).toContain("写：admin")
    vi.unstubAllGlobals()
  })

  it("关键词按签名名称前端过滤", async () => {
    mockFetch([pendingSign, approvedSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-keyword']").setValue("青鸾")
    await flushPromises()
    expect(wrapper.findAll(".sign-table .el-table__row")).toHaveLength(1)
    expect(wrapper.get(".sign-table").text()).toContain("青鸾平台")
    expect(wrapper.get(".sign-foot").text()).toContain("共 1 个签名")
    vi.unstubAllGlobals()
  })

  it("非管理员看不到申请与行内写操作，打开详情也不发起应用联查", async () => {
    const fetchMock = mockFetch([pendingSign])
    const pinia = applyRole("operator")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='new-sign']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-sync-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-delete-1']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='mobile-sign-sync-1']").exists()).toBe(false)
    expect(wrapper.text()).toContain("【待审签名】")

    const detailDrawer = wrapper.findAllComponents({ name: "ElDrawer" })[0]
    expect(detailDrawer.props("modelValue")).toBe(false)
    await wrapper.get("[data-testid='sign-detail-1']").trigger("keydown", { key: " " })
    await flushPromises()
    expect(detailDrawer.props("modelValue")).toBe(true)
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/admin/apps")).length).toBe(0)
    expect(document.body.textContent).not.toContain("默认签名引用")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("已通过签名仍可同步厂商状态，但不可编辑或删除", async () => {
    mockFetch([approvedSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='sign-sync-2']").text()).toContain("同步")
    expect(wrapper.find("[data-testid='sign-edit-2']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='sign-delete-2']").exists()).toBe(false)
    const actionCell = wrapper.get(".sign-table .el-table__row td:last-child")
    expect(actionCell.text()).toContain("不可编辑/删除")
    expect(actionCell.get(".muted").attributes("title")).toContain("已通过审核")
    expect(wrapper.get(".sign-mobile-list article footer").text()).toContain("不可编辑/删除")
    vi.unstubAllGlobals()
  })

  it("已拒绝签名保留厂商编号时仍可手动同步", async () => {
    mockFetch([{ ...rejectedSign, vendor_sign_id: "22" }])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='sign-sync-3']").text()).toContain("同步")
    vi.unstubAllGlobals()
  })

  it("编辑器内联提示方括号并在修正后清除，规范化预览同步重算", async () => {
    mockFetch([pendingSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-sign']").trigger("click")
    await flushPromises()
    const input = document.querySelector(".el-drawer input") as HTMLInputElement
    expect(input).toBeTruthy()
    expect(document.body.textContent).toContain("输入名称后实时预览规范化结果")

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
    expect(document.body.textContent).toContain("下发与计费：【青鸾平台】")
    expect(document.body.textContent).toContain("+6 字")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("名称为空或含方括号时不发起提交请求", async () => {
    const fetchMock = mockFetch([pendingSign])
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

  it("驳回签名重新提交时回显上次厂商驳回原因", async () => {
    mockFetch([rejectedSign])
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-edit-3']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("上次厂商驳回：签名与已有品牌近似")
    expect(document.body.textContent).toContain("平台 #3 · 重新提交将生成新的厂商编号")
    expect(document.body.textContent).toContain("重新提交审核")
    expect(document.body.textContent).toContain("提交后进入厂商人工审核，期间不可修改或删除")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("取消删除确认框不发起请求也不报错", async () => {
    const fetchMock = mockFetch([rejectedSign])
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const error = vi.spyOn(ElMessage, "error")
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-delete-3']").trigger("click")
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalled()
    const confirmMessage = vnodeText(vi.mocked(ElMessageBox.confirm).mock.calls[0][0])
    expect(confirmMessage).toContain("写入审计日志")
    expect(confirmMessage).toContain("被应用设为默认签名或已被批次引用的签名不可删除")
    expect(error).not.toHaveBeenCalled()
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "DELETE").length).toBe(0)
    vi.unstubAllGlobals()
  })

  it("同步失败时给出错误反馈且按钮恢复可用", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/sync")) {
        return {
          ok: false,
          status: 502,
          headers: { get: () => null },
          json: async () => ({ code: "VENDOR_ERROR", message: "厂商签名接口不可用", detail: null }),
        }
      }
      return okJson([pendingSign])
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

  it("详情抽屉展示审核轨迹、发送效果预览与 admin 默认签名引用", async () => {
    mockFetch(
      [approvedSign],
      [
        { id: 1, name: "订单中心", default_sign: "青鸾平台", status: 1 },
        { id: 2, name: "物流通知", default_sign: "其他签名", status: 1 },
        { id: 3, name: "会员服务", default_sign: "【青鸾平台】", status: 0 },
      ],
    )
    const pinia = applyRole("admin")
    const wrapper = mount(SignView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='sign-detail-2']").trigger("click")
    await flushPromises()

    const trail = document.querySelector("[data-testid='sign-trail']") as HTMLElement
    expect(trail.textContent).toContain("已提交送审")
    expect(trail.textContent).toContain("已绑定厂商编号")
    expect(trail.textContent).toContain("#21")
    expect(trail.textContent).toContain("厂商审核：已通过")

    expect(document.body.textContent).toContain("【青鸾平台】您的验证码为")
    expect(document.body.textContent).toContain("+6 字")

    const appsBlock = document.querySelector("[data-testid='sign-apps']") as HTMLElement
    expect(appsBlock.textContent).toContain("订单中心")
    expect(appsBlock.textContent).toContain("会员服务（已停用）")
    expect(appsBlock.textContent).not.toContain("物流通知")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
