import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage } from "element-plus"
import { readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"
import { vi } from "vitest"

describe("共享语义展示组件", () => {
  it("CategoryTag 集中呈现类别中文名与固定色类", async () => {
    const CategoryTag = (await import("../src/components/CategoryTag.vue")).default

    for (const [category, label] of [
      ["verify", "验证码"],
      ["notice", "通知"],
      ["market", "营销"],
    ] as const) {
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

    const terminal = mount(StatusTag, {
      props: { status: "unknown_terminal" },
      global: { plugins: [ElementPlus] },
    })
    expect(terminal.text()).toBe("未知终态")
    expect(terminal.getComponent({ name: "ElTag" }).props("effect")).toBe("dark")

    const completedUnknown = mount(StatusTag, {
      props: { status: "completed_unknown" },
      global: { plugins: [ElementPlus] },
    })
    expect(completedUnknown.text()).toBe("完成(含未知)")
    expect(completedUnknown.getComponent({ name: "ElTag" }).props("effect")).toBe("dark")

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

    const splitBlocked = mount(StatusTag, {
      props: { status: "split_capacity_blocked" },
      global: { plugins: [ElementPlus] },
    })
    expect(splitBlocked.text()).toBe("拆分容量阻塞")
    expect(splitBlocked.getComponent({ name: "ElTag" }).props("type")).toBe("warning")

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

  it("PhoneReveal 默认展示掩码与授权查看入口", async () => {
    const PhoneReveal = (await import("../src/components/PhoneReveal.vue")).default
    const reveal = vi.fn<() => Promise<string>>().mockResolvedValue("13800138000")
    const wrapper = mount(PhoneReveal, {
      props: { masked: "138****8000", reveal, testid: "phone-reveal-test" },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.text()).not.toContain("13800138000")
    expect(wrapper.find(".phone-mask").exists()).toBe(true)
    const button = wrapper.get("[data-testid='phone-reveal-test']")
    expect(button.text()).toContain("授权查看")
    expect(reveal).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("PhoneReveal 授权查看成功后内联展示明文并提示已记审计", async () => {
    const PhoneReveal = (await import("../src/components/PhoneReveal.vue")).default
    const success = vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
    const reveal = vi.fn<() => Promise<string>>().mockResolvedValue("13800138000")
    const wrapper = mount(PhoneReveal, {
      props: { masked: "138****8000", reveal, testid: "phone-reveal-test" },
      global: { plugins: [ElementPlus] },
    })

    await wrapper.get("[data-testid='phone-reveal-test']").trigger("click")
    await flushPromises()

    expect(reveal).toHaveBeenCalledTimes(1)
    expect(wrapper.get(".revealed-phone").text()).toBe("13800138000")
    expect(wrapper.find("[data-testid='phone-reveal-test']").exists()).toBe(false)
    expect(wrapper.emitted("revealed")).toEqual([["13800138000"]])
    expect(success).toHaveBeenCalledWith("已解密 · 本次授权查看已记入审计")
    wrapper.unmount()
    success.mockRestore()
  })

  it("PhoneReveal 解密失败给出错误提示且不展示明文", async () => {
    const PhoneReveal = (await import("../src/components/PhoneReveal.vue")).default
    const error = vi.spyOn(ElMessage, "error").mockImplementation(() => ({ close: () => undefined }))
    const reveal = vi.fn<() => Promise<string>>().mockRejectedValue(new Error("无解密权限"))
    const wrapper = mount(PhoneReveal, {
      props: { masked: "138****8000", reveal },
      global: { plugins: [ElementPlus] },
    })

    await wrapper.get("button").trigger("click")
    await flushPromises()

    expect(error).toHaveBeenCalledWith("无解密权限")
    expect(wrapper.find(".revealed-phone").exists()).toBe(false)
    expect(wrapper.text()).toContain("138****8000")
    // 失败后入口仍可重试
    expect(wrapper.get("button").text()).toContain("授权查看")
    wrapper.unmount()
    error.mockRestore()
  })

  it("所有业务页面使用统一的无插画双行空态", () => {
    const viewDirectory = resolve(process.cwd(), "src/views")
    const legacyViews = readdirSync(viewDirectory)
      .filter((name) => name.endsWith(".vue"))
      .filter((name) => readFileSync(resolve(viewDirectory, name), "utf8").includes("<el-empty"))

    expect(legacyViews).toEqual([])
  })
})
