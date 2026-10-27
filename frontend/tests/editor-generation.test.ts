import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import BlacklistView from "../src/views/BlacklistView.vue"
import SensitiveWordView from "../src/views/SensitiveWordView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe("添加器编辑代际", () => {
  it("敏感词按码点判断 64 字上限", async () => {
    const config = {
      key: "sensitive_hit_action",
      value: "block",
      value_type: "str",
      description: "策略",
      group: "发送策略",
      sensitive: false,
      configured: true,
      beat_restart_required: false,
      updated_by: null,
      updated_at: "2026-10-27T08:00:00+08:00",
      default: "block",
      min_value: null,
      max_value: null,
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/admin/configs")) return response([config])
        return response({ total: 0, items: [] })
      }),
    )
    const wrapper = mount(SensitiveWordView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    await wrapper.get("[data-testid='sensitive-add-open']").trigger("click")
    await flushPromises()
    const textarea = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    textarea.value = "😀".repeat(64)
    textarea.dispatchEvent(new Event("input"))
    await flushPromises()
    expect((document.querySelector("[data-testid='sensitive-add']") as HTMLButtonElement).disabled).toBe(false)
    textarea.value = "😀".repeat(65)
    textarea.dispatchEvent(new Event("input"))
    await flushPromises()
    expect((document.querySelector("[data-testid='sensitive-add']") as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("敏感词旧请求成功不会关闭或清空新草稿", async () => {
    const pending = deferred<ReturnType<typeof response>>()
    const config = {
      key: "sensitive_hit_action",
      value: "block",
      value_type: "str",
      description: "策略",
      group: "发送策略",
      sensitive: false,
      configured: true,
      beat_restart_required: false,
      updated_by: null,
      updated_at: "2026-10-27T08:00:00+08:00",
      default: "block",
      min_value: null,
      max_value: null,
    }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/configs")) return response([config])
      if (url.endsWith("/admin/sensitive-words") && init?.method === "POST") return pending.promise
      return response({ total: 0, items: [] })
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(SensitiveWordView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    await wrapper.get("[data-testid='sensitive-add-open']").trigger("click")
    await flushPromises()
    const first = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    first.value = "甲词"
    first.dispatchEvent(new Event("input"))
    await flushPromises()
    ;(document.querySelector("[data-testid='sensitive-add']") as HTMLButtonElement).click()
    await flushPromises()

    await wrapper.get("[data-testid='sensitive-add-open']").trigger("click")
    await flushPromises()
    const second = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    second.value = "乙词"
    second.dispatchEvent(new Event("input"))
    await flushPromises()
    const submit = document.querySelector("[data-testid='sensitive-add']") as HTMLButtonElement
    expect(submit.disabled).toBe(false)

    pending.resolve(response({ added: 1, skipped: 0, items: [] }))
    await flushPromises()
    const kept = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    expect(kept).toBeTruthy()
    expect(kept.value).toBe("乙词")
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST")
    expect(posts).toHaveLength(1)
    expect(JSON.parse(String(posts[0][1]?.body))).toEqual({ words: ["甲词"] })
    wrapper.unmount()
  })

  it("黑名单旧请求成功不会关闭或清空新草稿", async () => {
    const pending = deferred<ReturnType<typeof response>>()
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/admin/blacklist") && init?.method === "POST") return pending.promise
      return response({ total: 0, items: [] })
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(BlacklistView, {
      attachTo: document.body,
      global: { plugins: [createPinia(), ElementPlus] },
    })
    await flushPromises()
    await wrapper.get("[data-testid='blacklist-add-open']").trigger("click")
    await flushPromises()
    const first = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    first.value = "13800138000"
    first.dispatchEvent(new Event("input"))
    await flushPromises()
    ;(document.querySelector("[data-testid='blacklist-add']") as HTMLButtonElement).click()
    await flushPromises()

    await wrapper.get("[data-testid='blacklist-add-open']").trigger("click")
    await flushPromises()
    const second = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    second.value = "13800138001"
    second.dispatchEvent(new Event("input"))
    await flushPromises()
    expect((document.querySelector("[data-testid='blacklist-add']") as HTMLButtonElement).disabled).toBe(false)

    pending.resolve(response({ added: 1, updated: 0 }))
    await flushPromises()
    const kept = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
    expect(kept.value).toBe("13800138001")
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST")
    expect(JSON.parse(String(posts[0][1]?.body)).phones).toEqual(["13800138000"])
    wrapper.unmount()
  })
})
