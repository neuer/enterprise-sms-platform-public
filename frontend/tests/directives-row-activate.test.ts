import { mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"
import { defineComponent } from "vue"
import { vRowActivate } from "../src/lib/directives"

describe("v-row-activate 行内键盘激活指令", () => {
  it("Enter/Space 触发并阻断冒泡，其它键不触发", async () => {
    const spy = vi.fn()
    const parent = vi.fn()
    const Comp = defineComponent({
      directives: { "row-activate": vRowActivate },
      setup: () => ({ spy, parent }),
      template: `<div @keydown="parent"><button data-testid="t" v-row-activate="() => spy()">x</button></div>`,
    })
    const wrapper = mount(Comp)
    const target = wrapper.get("[data-testid='t']")
    await target.trigger("keydown.enter")
    expect(spy).toHaveBeenCalledTimes(1)
    expect(parent).not.toHaveBeenCalled()
    await target.trigger("keydown", { key: " " })
    expect(spy).toHaveBeenCalledTimes(2)
    await target.trigger("keydown", { key: "Enter" })
    expect(spy).toHaveBeenCalledTimes(3)
    await target.trigger("keydown", { key: "a" })
    expect(spy).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it("挂在行容器上时不抢占内部控件的键盘事件", async () => {
    const rowSpy = vi.fn()
    const innerSpy = vi.fn()
    const Comp = defineComponent({
      directives: { "row-activate": vRowActivate },
      setup: () => ({ rowSpy, innerSpy }),
      template: `<table><tbody><tr v-row-activate="() => rowSpy()"><td><button data-testid="inner" @click="innerSpy">x</button></td></tr></tbody></table>`,
    })
    const wrapper = mount(Comp, { attachTo: document.body })
    const inner = wrapper.get("[data-testid='inner']")
    // 焦点在内部按钮上：Enter 走按钮自身 click，不触发行激活
    await inner.trigger("keydown.enter")
    expect(rowSpy).not.toHaveBeenCalled()
    // 焦点在行容器本身：Enter/Space 触发行激活
    await wrapper.get("tr").trigger("keydown.enter")
    expect(rowSpy).toHaveBeenCalledTimes(1)
    await wrapper.get("tr").trigger("keydown", { key: " " })
    expect(rowSpy).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
