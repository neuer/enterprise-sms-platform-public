import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, describe, expect, it, vi } from "vitest"

import { useSessionStore } from "../src/stores/session"
import SignView from "../src/views/SignView.vue"
import TemplateView from "../src/views/TemplateView.vue"

afterEach(() => vi.unstubAllGlobals())

describe.each([
  { kind: "template", component: TemplateView },
  { kind: "sign", component: SignView },
])("$kind 编辑会话", ({ kind, component }) => {
  it.each(["success", "error"])("新草稿可立即编辑，旧 %s 不能解除新提交的锁", async (ending) => {
    const pending: Array<{ resolve: (value: unknown) => void; reject: (error: Error) => void }> = []
    const ok = { ok: true, status: 200, headers: { get: () => null }, json: async () => [] }
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.method === "POST") return new Promise((resolve, reject) => pending.push({ resolve, reject }))
        return Promise.resolve(ok)
      }),
    )
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().apply("synthetic-token", {
      account_id: 1,
      identity_id: 1,
      provider_code: "local",
      username: "synthetic-admin",
      display_name: "测试管理员",
      dept: "测试",
      role: "admin",
    })
    const wrapper = mount(component, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
    try {
      await flushPromises()
      const open = async (name: string) => {
        await wrapper.get(`[data-testid='new-${kind}']`).trigger("click")
        await flushPromises()
        const input = wrapper.get<HTMLInputElement>(`.${kind}-form input`)
        // 先断言真实 disabled；setValue 本身不能替代浏览器可操作性检查。
        expect(input.element.disabled).toBe(false)
        await input.setValue(name)
        if (kind === "template") await wrapper.get(".template-form textarea").setValue("合成模板 {1}")
      }
      await open("草稿甲")
      await wrapper.get(`[data-testid='${kind}-submit']`).trigger("click")
      await flushPromises()
      expect(pending).toHaveLength(1)
      expect(wrapper.get<HTMLInputElement>(`.${kind}-form input`).element.disabled).toBe(true)
      await wrapper.get(`.${kind}-editor-foot button:first-child`).trigger("click")
      await flushPromises()
      await open("草稿乙")
      await wrapper.get(`[data-testid='${kind}-submit']`).trigger("click")
      await flushPromises()
      expect(pending).toHaveLength(2)
      if (ending === "success") pending[0].resolve(ok)
      else pending[0].reject(new Error("合成旧请求错误"))
      await flushPromises()
      const input = wrapper.get<HTMLInputElement>(`.${kind}-form input`)
      expect(input.element.value).toBe("草稿乙")
      expect(input.element.disabled).toBe(true)
      const drawers = wrapper.findAllComponents({ name: "ElDrawer" })
      expect(drawers.at(-1)?.props("modelValue")).toBe(true)
      pending[1].resolve(ok)
      await flushPromises()
      expect(drawers.at(-1)?.props("modelValue")).toBe(false)
    } finally {
      for (const request of pending) request.resolve(ok)
      await flushPromises()
      wrapper.unmount()
    }
  })
})
