import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { describe, expect, it } from "vitest"

import CodePointInput from "../src/components/CodePointInput.vue"

describe("码点输入", () => {
  it("拒绝超过上限的非 BMP 粘贴并保留原值", async () => {
    const wrapper = mount(CodePointInput, {
      props: { modelValue: "🚀🚀", maxCodePoints: 2, multiline: true, testId: "content" },
      global: { plugins: [ElementPlus] },
    })
    wrapper.findComponent({ name: "ElInput" }).vm.$emit("update:modelValue", "🚀🚀🚀")
    await flushPromises()
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
    expect(wrapper.get("[data-testid='code-point-exceeded']").text()).toContain("2")

    wrapper.findComponent({ name: "ElInput" }).vm.$emit("update:modelValue", "🚀")
    await flushPromises()
    expect(wrapper.emitted("update:modelValue")?.[0]).toEqual(["🚀"])
    wrapper.unmount()
  })
})
