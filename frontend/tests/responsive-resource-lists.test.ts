import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, vi } from "vitest"

import { useSessionStore } from "../src/stores/session"
import SignView from "../src/views/SignView.vue"
import TemplateView from "../src/views/TemplateView.vue"

enableAutoUnmount(afterEach)
afterEach(() => vi.unstubAllGlobals())

function viewport(initialWidth: number) {
  let width = initialWidth
  const media = new EventTarget() as MediaQueryList
  Object.defineProperties(media, {
    matches: { get: () => width <= 760 },
    media: { value: "(max-width: 760px)" },
  })
  const add = vi.spyOn(media, "addEventListener")
  const remove = vi.spyOn(media, "removeEventListener")
  const matchMedia = vi.fn().mockImplementation((query: string) => {
    if (query === media.media) return media
    // Element Plus 抽屉也查询动画偏好；它与列表断点使用独立监听目标。
    const other = new EventTarget()
    Object.defineProperties(other, { matches: { value: false }, media: { value: query } })
    return other
  })
  vi.stubGlobal("matchMedia", matchMedia)
  return {
    add,
    remove,
    matchMedia,
    resize(nextWidth: number): void {
      width = nextWidth
      const event = new Event("change")
      Object.defineProperty(event, "matches", { value: media.matches })
      media.dispatchEvent(event)
    },
  }
}

const resources = [
  {
    kind: "template",
    component: TemplateView,
    desktopDetail: "template-detail-1",
    mobileDetail: "template-mobile-detail-1",
    row: { content: "验证码{1}", var_specs: [{ pos: 1, max_len: 6 }], vendor_template_id: null },
  },
  {
    kind: "sign",
    component: SignView,
    desktopDetail: "sign-detail-1",
    mobileDetail: "mobile-sign-detail-1",
    row: { vendor_sign_id: null },
  },
] as const

function session(role: "admin" | "approver") {
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

function mockRows(resource: (typeof resources)[number], count: number) {
  const rows = Array.from({ length: count }, (_, index) => ({
    ...resource.row,
    id: index + 1,
    name: index === 0 ? "保留目标" : `资源 ${index + 1}`,
    vendor_state: index % 2 === 0 ? "rejected" : "approved",
    vendor_reject_reason: null,
  }))
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, _init?: RequestInit) => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => (String(input).includes("/admin/apps") ? [] : rows),
  }))
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

function expectRows(wrapper: VueWrapper, kind: string, mobile: boolean, count: number): void {
  expect(wrapper.find(`.${kind}-table`).exists()).toBe(!mobile)
  expect(wrapper.find(`.${kind}-mobile-list`).exists()).toBe(mobile)
  expect(wrapper.findAllComponents({ name: "ElTable" })).toHaveLength(mobile ? 0 : 1)
  expect(wrapper.findAll(`.${kind}-table .el-table__row`)).toHaveLength(mobile ? 0 : count)
  expect(wrapper.findAll(`.${kind}-mobile-list > article`)).toHaveLength(mobile ? count : 0)
}

describe.each(resources)("$kind 响应式列表", (resource) => {
  it.each([
    [100, 761],
    [100, 760],
    [1000, 761],
    [1000, 760],
  ])("%i 行在 %ipx 初次挂载与断点切换时只有一套行树", async (count, width) => {
    const screen = viewport(width)
    const fetchMock = mockRows(resource, count)
    const wrapper = mount(resource.component, { global: { plugins: [session("approver"), ElementPlus] } })
    await flushPromises()

    const mobile = width <= 760
    expect(screen.matchMedia).toHaveBeenCalledWith("(max-width: 760px)")
    expectRows(wrapper, resource.kind, mobile, count)

    screen.resize(mobile ? 761 : 760)
    await flushPromises()
    expectRows(wrapper, resource.kind, !mobile, count)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    expect(screen.add).toHaveBeenCalledTimes(1)
    expect(screen.remove).toHaveBeenCalledWith("change", screen.add.mock.calls[0][1])
  })

  it("保留状态和关键词筛选、键盘详情、打开的抽屉与未提交表单", async () => {
    const screen = viewport(761)
    const fetchMock = mockRows(resource, 100)
    const wrapper = mount(resource.component, {
      attachTo: document.body,
      global: { plugins: [session("admin"), ElementPlus] },
    })
    await flushPromises()

    await wrapper.get(`[data-testid='${resource.kind}-state-rejected']`).trigger("click")
    await wrapper.get(`[data-testid='${resource.kind}-keyword']`).setValue("保留目标")
    expectRows(wrapper, resource.kind, false, 1)
    const desktopTrigger = wrapper.get(`[data-testid='${resource.desktopDetail}']`)
    expect(desktopTrigger.attributes("aria-label")).toContain("保留目标")
    await desktopTrigger.trigger("keydown", { key: "Enter" })
    await flushPromises()
    const detail = wrapper.findAllComponents({ name: "ElDrawer" })[0]
    expect(detail.props("modelValue")).toBe(true)

    screen.resize(760)
    await flushPromises()
    expectRows(wrapper, resource.kind, true, 1)
    expect(wrapper.get(`[data-testid='${resource.kind}-state-rejected']`).classes()).toContain("on")
    expect((wrapper.get(`.${resource.kind}-keyword input`).element as HTMLInputElement).value).toBe("保留目标")
    expect(wrapper.findAllComponents({ name: "ElDrawer" })[0].vm).toBe(detail.vm)
    expect(detail.props("modelValue")).toBe(true)
    expect(detail.text()).toContain("保留目标")

    // 按抽屉 v-model 合同关闭，避免把 Element Plus 离场动画计时耦合到布局测试。
    detail.vm.$emit("update:modelValue", false)
    await flushPromises()
    expect(detail.props("modelValue")).toBe(false)
    const mobileTrigger = wrapper.get(`[data-testid='${resource.mobileDetail}']`)
    expect(mobileTrigger.attributes("aria-label")).toContain("保留目标")
    await mobileTrigger.trigger("keydown", { key: " " })
    await flushPromises()
    expect(detail.props("modelValue")).toBe(true)
    await wrapper.get(`[data-testid='${resource.kind}-detail-edit']`).trigger("click")
    await flushPromises()
    const drawers = wrapper.findAllComponents({ name: "ElDrawer" })
    const editor = drawers[drawers.length - 1]
    expect(editor.props("modelValue")).toBe(true)
    await editor.get("input").setValue("未提交草稿")

    screen.resize(761)
    await flushPromises()
    expectRows(wrapper, resource.kind, false, 1)
    expect(editor.props("modelValue")).toBe(true)
    expect((editor.get("input").element as HTMLInputElement).value).toBe("未提交草稿")
    expect(fetchMock.mock.calls.filter(([input]) => !String(input).includes("/admin/apps"))).toHaveLength(1)
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true)
  })
})
