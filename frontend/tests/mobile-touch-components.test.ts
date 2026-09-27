import { enableAutoUnmount, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { afterEach, describe, expect, it, vi } from "vitest"

import ListPagination from "../src/components/ListPagination.vue"
import MobileFilterToggle from "../src/components/MobileFilterToggle.vue"

enableAutoUnmount(afterEach)
afterEach(() => vi.unstubAllGlobals())

function viewport(width: number): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => {
      const media = new EventTarget()
      Object.defineProperties(media, {
        matches: { value: query === "(max-width: 760px)" && width <= 760 },
        media: { value: query },
      })
      return media
    }),
  )
}

describe("ListPagination 手机页码", () => {
  it("桌面渲染完整页码条", () => {
    viewport(1440)
    const wrapper = mount(ListPagination, { props: { page: 3, total: 640 }, global: { plugins: [ElementPlus] } })
    expect(wrapper.find(".el-pager").exists()).toBe(true)
    expect(wrapper.find(".list-pagination-pages").exists()).toBe(false)
  })

  it("手机收敛为「当前 / 总页数」，保留前后翻", () => {
    viewport(390)
    const wrapper = mount(ListPagination, { props: { page: 3, total: 640 }, global: { plugins: [ElementPlus] } })
    expect(wrapper.find(".el-pager").exists()).toBe(false)
    expect(wrapper.get(".list-pagination-pages").text()).toBe("3 / 32")
    expect(wrapper.find(".btn-prev").exists()).toBe(true)
    expect(wrapper.find(".btn-next").exists()).toBe(true)
  })

  it("空列表总页数按 1 计", () => {
    viewport(390)
    const wrapper = mount(ListPagination, { props: { page: 1, total: 0 }, global: { plugins: [ElementPlus] } })
    expect(wrapper.get(".list-pagination-pages").text()).toBe("1 / 1")
  })
})

describe("MobileFilterToggle", () => {
  it("折叠态提示被收起条件中的生效数，点击切换展开", async () => {
    const wrapper = mount(MobileFilterToggle, { props: { collapsed: true, activeCount: 2 } })
    const button = wrapper.get('[data-testid="mobile-filter-toggle"]')
    expect(button.attributes("aria-expanded")).toBe("false")
    expect(button.text()).toContain("更多条件")
    expect(button.get("b").text()).toBe("2")
    expect(button.classes()).toContain("is-active")

    await button.trigger("click")
    expect(wrapper.emitted("update:collapsed")).toEqual([[false]])
  })

  it("展开态不显示计数", () => {
    const wrapper = mount(MobileFilterToggle, { props: { collapsed: false, activeCount: 2 } })
    const button = wrapper.get('[data-testid="mobile-filter-toggle"]')
    expect(button.attributes("aria-expanded")).toBe("true")
    expect(button.text()).toContain("收起条件")
    expect(button.find("b").exists()).toBe(false)
  })
})
