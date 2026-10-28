import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"

import SignView from "../src/views/SignView.vue"
import TemplateView from "../src/views/TemplateView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return { ok: status < 400, status, headers: { get: () => null }, json: async () => body }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ""
})

const cases = ["sign", "template"].flatMap((page) =>
  (page === "sign" ? ["create", "sync", "delete", "adopt"] : ["create", "sync", "delete"]).flatMap((operation) =>
    ["unmount", "session-clear"].flatMap((boundary) =>
      ["success", "failure"].map((outcome) => ({ page, operation, boundary, outcome })),
    ),
  ),
)

it.each(cases)(
  "$page $operation 的迟到 $outcome 在 $boundary 后不通知或重读",
  async ({ page, operation, boundary, outcome }) => {
    let release!: (value: ReturnType<typeof response>) => void
    const pending = new Promise<ReturnType<typeof response>>((resolve) => {
      release = resolve
    })
    const entry = {
      id: 1,
      name: "合成测试",
      content: "无变量合成模板",
      var_specs: [],
      vendor_state: operation === "sync" ? "approved" : "pending",
      vendor_sign_id: operation === "sync" ? "1" : null,
      vendor_template_id: operation === "sync" ? "1" : null,
    }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method !== "GET") return pending
      return response(url.endsWith("/apps") ? [] : [entry])
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const pinia = createPinia()
    const session = useSessionStore(pinia)
    session.apply("test-access", {
      account_id: 1,
      identity_id: 1,
      provider_code: "local",
      username: "admin",
      display_name: "测试管理员",
      dept: "测试部门",
      role: "admin",
    })
    const wrapper = mount(page === "sign" ? SignView : TemplateView, {
      attachTo: document.body,
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    if (operation === "create") {
      await wrapper.get(`[data-testid='new-${page}']`).trigger("click")
      await flushPromises()
      const inputs = document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(
        ".el-drawer input, .el-drawer textarea",
      )
      for (const input of inputs) {
        input.value = "合成内容"
        input.dispatchEvent(new Event("input"))
      }
      await flushPromises()
      ;(document.querySelector(`[data-testid='${page}-submit']`) as HTMLButtonElement).click()
    } else {
      await wrapper.get(`[data-testid='${page}-${operation}-1']`).trigger("click")
      if (operation === "adopt") {
        await flushPromises()
        const input = document.querySelector("[data-testid='sign-adopt-vendor-id']") as HTMLInputElement
        input.value = "12"
        input.dispatchEvent(new Event("input"))
        await flushPromises()
        ;(document.querySelector("[data-testid='sign-adopt-submit']") as HTMLButtonElement).click()
      }
    }
    await flushPromises()
    expect(fetch.mock.calls.filter(([, init]) => init?.method !== "GET")).toHaveLength(1)
    const success = vi.spyOn(ElMessage, "success")
    const failure = vi.spyOn(ElMessage, "error")
    if (boundary === "unmount") wrapper.unmount()
    else session.clear()
    const reads = fetch.mock.calls.filter(([, init]) => init?.method === "GET").length
    release(
      outcome === "success"
        ? response(entry)
        : response({ code: "DEPENDENCY_UNAVAILABLE", message: "合成迟到失败" }, 503),
    )
    await flushPromises()
    expect(success).not.toHaveBeenCalled()
    expect(failure).not.toHaveBeenCalled()
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "GET")).toHaveLength(reads)
    if (boundary !== "unmount") wrapper.unmount()
  },
)
