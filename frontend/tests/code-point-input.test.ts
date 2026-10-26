import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { describe, expect, it } from "vitest"
import { defineComponent, ref } from "vue"

import CodePointInput from "../src/components/CodePointInput.vue"

describe("码点输入", () => {
  it.each([false, true])("组件结束组合后立即提交，不依赖另一个原生监听器（多行=%s）", async (multiline) => {
    const wrapper = mount(CodePointInput, {
      props: { modelValue: "原", maxCodePoints: 5, multiline },
      global: { plugins: [ElementPlus] },
    })
    try {
      const input = wrapper.get(multiline ? "textarea" : "input").element
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }))
      const field = wrapper.findComponent({ name: "ElInput" })
      // Chromium 可在两个原生监听器间执行微任务；组件提交不能等待后一个监听器。
      field.vm.$emit("compositionend", new CompositionEvent("compositionend", { data: "中文输入" }))
      await flushPromises()
      field.vm.$emit("update:modelValue", "原中文输入")
      await flushPromises()
      expect(wrapper.emitted("update:modelValue")).toEqual([["原中文输入"]])
      field.vm.$emit("update:modelValue", "原中文输入超")
      await flushPromises()
      expect(wrapper.emitted("update:modelValue")).toHaveLength(1)
      expect(wrapper.get("[role='alert']").text()).toContain("5")
    } finally {
      wrapper.unmount()
    }
  })

  it.each([false, true])("真实 Element Plus composition 提交保持模型与 DOM 一致（多行=%s）", async (multiline) => {
    const host = defineComponent({
      components: { CodePointInput },
      setup: () => ({ value: ref("原"), multiline }),
      template: '<CodePointInput v-model="value" :multiline="multiline" :max-code-points="10" />',
    })
    const wrapper = mount(host, { global: { plugins: [ElementPlus] } })
    try {
      await flushPromises()
      const input = wrapper.get<HTMLInputElement | HTMLTextAreaElement>(multiline ? "textarea" : "input").element
      input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }))
      input.dispatchEvent(new CompositionEvent("compositionupdate", { data: "中文输入", bubbles: true }))
      input.value = "原中文输入"
      input.dispatchEvent(new InputEvent("input", { data: "中文输入", isComposing: true, bubbles: true }))
      await flushPromises()
      expect(wrapper.vm.value).toBe("原")
      input.dispatchEvent(new CompositionEvent("compositionend", { data: "中文输入", bubbles: true }))
      await flushPromises()
      expect(wrapper.vm.value).toBe("原中文输入")
      expect(input.value).toBe("原中文输入")
    } finally {
      wrapper.unmount()
    }
  })

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
