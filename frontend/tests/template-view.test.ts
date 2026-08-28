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
  useSessionStore().apply("jwt", {
    account_id: 1,
    identity_id: 11,
    provider_code: "local",
    username: `${role}01`, display_name: "测试用户", dept: "平台部", role,
  })
  return pinia
}

function mockList(payload: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => payload,
    }),
  )
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

/** VTU mocks 不写入 appContext.globalProperties，$router 必须以插件方式安装。 */
function routerPlugin(router: unknown) {
  return {
    install(app: { config: { globalProperties: Record<string, unknown> } }) {
      app.config.globalProperties.$router = router
    },
  }
}

describe("模板管理", () => {
  it("内容列内联渲染变量片，状态列区分待审核双阶段", async () => {
    mockList([template])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.find(".template-filter-bar").exists()).toBe(true)
    expect(wrapper.find(".filter-toolbar").exists()).toBe(false)
    expect(wrapper.get(".template-filter-note").text()).toBe("接口全量返回 · 前端过滤")
    const chip = wrapper.get(".template-table .var-chip")
    expect(chip.text()).toBe("{1}≤6")
    expect(chip.attributes("title")).toContain("最大 6 字")
    expect(wrapper.get(".template-table").text()).toContain("验证码")
    expect(wrapper.get(".template-table").text()).toContain("待审核")
    // 已绑定厂商编号的 pending 显示「厂商审核中」；未绑定显示「提交厂商中」
    expect(wrapper.get(".template-table").text()).toContain("厂商审核中 · #21")
    expect(wrapper.find(".status-tag--pending").exists()).toBe(true)
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

  it("待审核未绑定厂商编号时不提供手动同步入口", async () => {
    mockList([{ ...template, id: 9, vendor_template_id: null }])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get(".template-table").text()).toContain("提交厂商中…")
    expect(wrapper.find("[data-testid='template-sync-9']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-delete-9']").exists()).toBe(true)
    vi.unstubAllGlobals()
  })

  it("审批员只能查看模板且不会看到任何写操作", async () => {
    mockList([template])
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
    mockList([template])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='new-template']").text()).toContain("新建模板")
    expect(wrapper.get("[data-testid='template-sync-1']").text()).toContain("同步")
    vi.unstubAllGlobals()
  })

  it("所有已绑定厂商编号的审核状态均可手动同步，草稿不显示入口", async () => {
    const rejectedTemplate = {
      ...template,
      id: 2,
      vendor_state: "rejected",
      vendor_reject_reason: "初次审核未通过",
    }
    const draftTemplate = { ...template, id: 3, vendor_state: "draft" }
    const approvedTemplate = { ...template, id: 4, vendor_state: "approved" }
    const templates = [rejectedTemplate, draftTemplate, approvedTemplate]
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => ({
      ok: true,
      status: String(input).endsWith("/templates/2/sync") ? 202 : 200,
      headers: { get: () => null },
      json: async () => templates,
    }))
    vi.stubGlobal("fetch", fetchMock)
    vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get("[data-testid='template-sync-2']").text()).toContain("同步")
    expect(wrapper.find("[data-testid='template-sync-3']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='template-sync-4']").text()).toContain("同步")

    await wrapper.get("[data-testid='template-sync-2']").trigger("click")
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/web/templates/2/sync",
      expect.objectContaining({ method: "POST" }),
    )
    vi.unstubAllGlobals()
  })

  it("状态筛选包含草稿选项并展示各状态计数", async () => {
    mockList([template, { ...template, id: 5, vendor_state: "draft" }])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    const toolbar = wrapper.get(".template-filter-bar")
    expect(toolbar.text()).toContain("草稿")
    expect(toolbar.text()).toContain("待审核")
    expect(wrapper.get("[data-testid='template-state-pending']").text()).toContain("1")
    expect(wrapper.get("[data-testid='template-state-draft']").text()).toContain("1")
    expect(wrapper.get("[data-testid='template-state-all']").text()).toContain("2")

    await wrapper.get("[data-testid='template-state-draft']").trigger("click")
    await flushPromises()
    expect(wrapper.findAll(".template-table .el-table__row")).toHaveLength(1)
    expect(wrapper.get(".template-table").text()).toContain("未送审（历史数据）")
    expect(wrapper.get(".template-foot").text()).toContain("共 1 个模板")
    expect(wrapper.get(".template-foot-role").text()).toContain("读：operator / approver / admin")
    vi.unstubAllGlobals()
  })

  it("关键词按名称与内容前端过滤", async () => {
    mockList([template, { ...template, id: 6, name: "停机公告", content: "系统维护{1}" }])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='template-keyword']").setValue("停机")
    await flushPromises()
    expect(wrapper.findAll(".template-table .el-table__row")).toHaveLength(1)
    expect(wrapper.get(".template-table").text()).toContain("停机公告")
    expect(wrapper.get(".template-foot").text()).toContain("共 1 个模板")
    vi.unstubAllGlobals()
  })

  it("已通过模板可用于发送并可同步厂商状态，但不可编辑或删除", async () => {
    mockList([{ ...template, id: 3, vendor_state: "approved" }])
    const push = vi.fn()
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, {
      global: {
        plugins: [pinia, ElementPlus, routerPlugin({ push, currentRoute: { value: { query: {} } } })],
      },
    })
    await flushPromises()

    expect(wrapper.get("[data-testid='template-sync-3']").text()).toContain("同步")
    expect(wrapper.find("[data-testid='template-edit-3']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-delete-3']").exists()).toBe(false)
    const use = wrapper.get("[data-testid='template-use-3']")
    expect(use.text()).toContain("用于发送")
    await use.trigger("click")
    expect(push).toHaveBeenCalledWith({ path: "/send", query: { template_id: "3" } })
    vi.unstubAllGlobals()
  })

  it("部门列仅 admin 渲染，为空时列表与移动卡片均显示占位", async () => {
    mockList([{ ...template, id: 4, dept: "" }])
    const operatorPinia = applyRole("operator")
    const operatorWrapper = mount(TemplateView, { global: { plugins: [operatorPinia, ElementPlus] } })
    await flushPromises()
    expect(operatorWrapper.get(".template-table thead").text()).not.toContain("部门")
    expect(operatorWrapper.find(".template-mobile-list dl").exists()).toBe(false)
    operatorWrapper.unmount()

    const adminPinia = applyRole("admin")
    const wrapper = mount(TemplateView, { global: { plugins: [adminPinia, ElementPlus] } })
    await flushPromises()

    // 列序：名称 / 内容 / 部门 / 厂商状态 / 操作
    const deptCell = wrapper.get(".template-table .el-table__row td:nth-child(3)")
    expect(deptCell.text()).toBe("—")
    expect(deptCell.get("span").classes()).toContain("muted")
    expect(wrapper.get(".template-mobile-list dl > div:last-child dd").text()).toBe("—")
    vi.unstubAllGlobals()
  })

  it("编辑器变量随内容自动识别并保留已设长度，非法与断档占位内联报错", async () => {
    mockList([template])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    await wrapper.get("[data-testid='new-template']").trigger("click")
    await flushPromises()
    const textarea = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    expect(textarea).toBeTruthy()

    // 输入合法占位：变量行自动识别，厂商格式预览实时重算，不再依赖手动「从内容识别」
    textarea.value = "尊敬的{1}，验证码{2}"
    textarea.dispatchEvent(new Event("input"))
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).not.toContain("未声明变量最大长度")
    expect(document.querySelectorAll(".variable-row")).toHaveLength(2)
    expect(document.body.textContent).toContain("尊敬的{s10}，验证码{s10}")

    // 跳号占位：提示必须从 {1} 连续编号，变量行跟随内容只剩 {2}
    textarea.value = "验证码{2}"
    textarea.dispatchEvent(new Event("input"))
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).toContain("必须从 {1} 开始连续编号")
    expect(document.querySelectorAll(".variable-row")).toHaveLength(1)

    // 非法 {} 片段：内联报错且变量行清空
    textarea.value = "验证码{}"
    textarea.dispatchEvent(new Event("input"))
    await new Promise((resolve) => setTimeout(resolve, 150))
    await flushPromises()
    expect(document.body.textContent).toContain("仅允许使用 {1}..{n} 格式占位")
    expect(document.querySelectorAll(".variable-row")).toHaveLength(0)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("已绑定的驳回模板保留原因且要求新建", async () => {
    mockList([{ ...template, id: 7, vendor_state: "rejected", vendor_reject_reason: "含未报备营销内容" }])
    const pinia = applyRole("operator")
    const wrapper = mount(TemplateView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.get(".template-table").text()).toContain("含未报备营销内容")
    expect(wrapper.find("[data-testid='template-edit-7']").exists()).toBe(false)
    expect(wrapper.find("[data-testid='template-delete-7']").exists()).toBe(false)
    await wrapper.get("[data-testid='template-detail-7']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("不能原地修改；请新建模板")
    expect(document.body.textContent).toContain("被拒绝或撤销后请新建模板")
    wrapper.unmount()
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
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("取消删除确认框不发起请求也不报错", async () => {
    const rejectedTemplate = {
      ...template,
      id: 2,
      name: "通知",
      vendor_template_id: null,
      vendor_state: "rejected",
    }
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
    const confirmMessage = vnodeText(vi.mocked(ElMessageBox.confirm).mock.calls[0][0])
    expect(confirmMessage).toContain("写入审计日志")
    expect(confirmMessage).toContain("已绑定厂商编号或已被批次引用的模板不可删除")
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
