import { mount } from "@vue/test-utils"
import ElementPlus from "element-plus"

import FilterSeg from "../src/components/FilterSeg.vue"
import ListPagination from "../src/components/ListPagination.vue"

function mountInWorkspace(component: unknown, options: Record<string, unknown>) {
  return mount(component as never, {
    ...options,
    global: { plugins: [ElementPlus] },
  })
}

describe("ListPagination", () => {
  it("渲染计数文案（默认单位「条」、默认每页 20）与分页器", () => {
    const wrapper = mountInWorkspace(ListPagination, { props: { page: 1, total: 45 } })
    expect(wrapper.text()).toContain("共 45 条 · 每页 20")
    expect(wrapper.getComponent({ name: "ElPagination" }).props("total")).toBe(45)
  })

  it("单位词与每页条数经 props 承载差异", () => {
    const wrapper = mountInWorkspace(ListPagination, {
      props: { page: 2, total: 7, pageSize: 60, unit: "名用户" },
    })
    expect(wrapper.text()).toContain("共 7 名用户 · 每页 60")
  })

  it("翻页时先回写 page 再发出 change", async () => {
    const wrapper = mountInWorkspace(ListPagination, {
      props: { page: 1, total: 45, testid: "demo-pagination" },
    })
    await wrapper.get("[data-testid='demo-pagination'] .btn-next").trigger("click")
    expect(wrapper.emitted("update:page")).toEqual([[2]])
    expect(wrapper.emitted("change")).toHaveLength(1)
  })

  it("showCount=false 时不渲染计数（统计报表明细等仅翻页场景）", () => {
    const wrapper = mountInWorkspace(ListPagination, { props: { page: 1, total: 45, showCount: false } })
    expect(wrapper.text()).not.toContain("共 45")
    expect(wrapper.findComponent({ name: "ElPagination" }).exists()).toBe(true)
  })

  it("before / 默认插槽按位渲染，countTail 片段拼接在计数文案后", () => {
    const wrapper = mountInWorkspace(ListPagination, {
      props: { page: 1, total: 45, unit: "项", countTail: "· dead 总计 3" },
      slots: {
        before: "<i class='legend-slot'>图例</i>",
        default: "<b class='middle-slot'>筛选</b>",
      },
    })
    expect(wrapper.find(".legend-slot").exists()).toBe(true)
    expect(wrapper.find(".middle-slot").exists()).toBe(true)
    expect(wrapper.text()).toContain("共 45 项 · 每页 20 · dead 总计 3")
  })
})

describe("FilterSeg", () => {
  const options = [
    { label: "全部", value: "", key: "all" },
    { label: "人工加入", value: "manual" },
    { label: "导入", value: "import", testid: "custom-import" },
  ]

  it("渲染 role=group 与选项按钮，激活项带 on 类；透传 aria-label 与 data-testid 到组容器", () => {
    const wrapper = mount(FilterSeg, {
      props: { modelValue: "manual", options },
      attrs: { "aria-label": "来源筛选", "data-testid": "demo-seg" },
    })
    const group = wrapper.get("[role='group']")
    expect(group.attributes("aria-label")).toBe("来源筛选")
    expect(group.attributes("data-testid")).toBe("demo-seg")
    const buttons = wrapper.findAll("button")
    expect(buttons).toHaveLength(3)
    expect(buttons[1].classes()).toContain("on")
    expect(buttons[0].classes()).not.toContain("on")
  })

  it("按钮 testid 由前缀与 key 推导，option.testid 可覆盖", () => {
    const wrapper = mount(FilterSeg, {
      props: { modelValue: "", options, buttonTestidPrefix: "demo-source" },
    })
    const testids = wrapper.findAll("button").map((button) => button.attributes("data-testid"))
    expect(testids).toEqual(["demo-source-all", "demo-source-manual", "custom-import"])
  })

  it("点击非激活项 emit 新值；点击当前项不重复 emit", async () => {
    const wrapper = mount(FilterSeg, { props: { modelValue: "", options } })
    const buttons = wrapper.findAll("button")
    await buttons[2].trigger("click")
    expect(wrapper.emitted("update:modelValue")).toEqual([["import"]])
    await wrapper.setProps({ modelValue: "import" })
    await buttons[2].trigger("click")
    expect(wrapper.emitted("update:modelValue")).toHaveLength(1)
  })

  it("整体或单项 disabled 时不 emit", async () => {
    const wrapper = mount(FilterSeg, { props: { modelValue: "", options, disabled: true } })
    await wrapper.findAll("button")[1].trigger("click")
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })

  it("#option 插槽渲染计数等附加内容，prefix/suffix 插槽就位", () => {
    const wrapper = mount(FilterSeg, {
      props: {
        modelValue: "a",
        options: [
          { label: "甲", value: "a", count: 3 },
          { label: "乙", value: "b", count: null, class: "hot" },
        ],
      },
      slots: {
        prefix: "<span class='demo-lbl'>状态</span>",
        suffix: "<span class='demo-meta'>说明</span>",
        option: `<template #option="{ option }">{{ option.label }}<i v-if="option.count !== null && option.count !== undefined">{{ option.count }}</i></template>`,
      },
    })
    expect(wrapper.find(".demo-lbl").exists()).toBe(true)
    expect(wrapper.find(".demo-meta").exists()).toBe(true)
    expect(wrapper.findAll("button")[0].text()).toContain("3")
    expect(wrapper.findAll("button")[1].classes()).toContain("hot")
  })

  it("class 透传合并到根节点（变体类承载紧凑/胶囊/分组样式）", () => {
    const wrapper = mount(FilterSeg, {
      props: { modelValue: "", options },
      attrs: { class: "filter-seg--compact" },
    })
    const root = wrapper.get("[role='group']")
    expect(root.classes()).toContain("filter-seg")
    expect(root.classes()).toContain("filter-seg--compact")
  })
})
