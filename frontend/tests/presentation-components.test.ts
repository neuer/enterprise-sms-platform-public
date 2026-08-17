import { mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

describe("共享语义展示组件", () => {
  it("CategoryTag 集中呈现类别中文名与固定色类", async () => {
    const CategoryTag = (await import("../src/components/CategoryTag.vue")).default

    for (const [category, label] of [["verify", "验证码"], ["notice", "通知"], ["market", "营销"]] as const) {
      const wrapper = mount(CategoryTag, {
        props: { category },
        global: { plugins: [ElementPlus] },
      })

      expect(wrapper.text()).toBe(label)
      expect(wrapper.classes()).toContain(`category-tag--${category}`)
    }
  })

  it("StatusTag 统一常用状态文案并保留未知状态", async () => {
    const StatusTag = (await import("../src/components/StatusTag.vue")).default

    const uncertain = mount(StatusTag, {
      props: { status: "uncertain" },
      global: { plugins: [ElementPlus] },
    })
    expect(uncertain.text()).toBe("结果未知")
    expect(uncertain.classes()).toContain("status-tag--uncertain")
    expect(uncertain.getComponent({ name: "ElTag" }).props("effect")).toBe("dark")
    expect(uncertain.getComponent({ name: "ElTag" }).props("type")).toBe("danger")

    const unknown = mount(StatusTag, {
      props: { status: "unknown" },
      global: { plugins: [ElementPlus] },
    })
    expect(unknown.text()).toBe("未知")
    expect(unknown.getComponent({ name: "ElTag" }).props("type")).toBe("danger")
    expect(unknown.getComponent({ name: "ElTag" }).props("effect")).toBe("plain")

    const blocked = mount(StatusTag, {
      props: { status: "balance_blocked" },
      global: { plugins: [ElementPlus] },
    })
    expect(blocked.text()).toBe("余额阻断")
    expect(blocked.getComponent({ name: "ElTag" }).props("type")).toBe("danger")
    expect(blocked.getComponent({ name: "ElTag" }).props("effect")).toBe("dark")

    const queued = mount(StatusTag, {
      props: { status: "queued" },
      global: { plugins: [ElementPlus] },
    })
    expect(queued.getComponent({ name: "ElTag" }).props("type")).toBe("info")

    const future = mount(StatusTag, {
      props: { status: "future_state" },
      global: { plugins: [ElementPlus] },
    })
    expect(future.text()).toBe("future_state")
  })

  it("EmptyState 使用无插画双行文案说明结论与下一步", async () => {
    const EmptyState = (await import("../src/components/EmptyState.vue")).default
    const wrapper = mount(EmptyState, {
      props: {
        title: "当前没有待审批记录",
        description: "新的审批申请会出现在这里。",
      },
    })

    expect(wrapper.attributes("role")).toBe("status")
    expect(wrapper.get("strong").text()).toBe("当前没有待审批记录")
    expect(wrapper.get("p").text()).toBe("新的审批申请会出现在这里。")
    expect(wrapper.find(".el-empty").exists()).toBe(false)
  })

  it("所有业务页面使用统一的无插画双行空态", () => {
    const viewDirectory = resolve(process.cwd(), "src/views")
    const legacyViews = readdirSync(viewDirectory)
      .filter((name) => name.endsWith(".vue"))
      .filter((name) => readFileSync(resolve(viewDirectory, name), "utf8").includes("<el-empty"))

    expect(legacyViews).toEqual([])
  })
})
