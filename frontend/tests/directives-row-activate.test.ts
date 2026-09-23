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
})
