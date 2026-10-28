import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage } from "element-plus"
import { createPinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"

import BlacklistView from "../src/views/BlacklistView.vue"
import SensitiveWordView from "../src/views/SensitiveWordView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return { ok: status < 400, status, headers: { get: () => null }, json: async () => body }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ""
})

it.each(
  ["blacklist", "sensitive"].flatMap((page) =>
    ["unmount", "session-clear"].flatMap((boundary) =>
      ["success", "failure"].map((outcome) => ({ page, boundary, outcome })),
    ),
  ),
)("$page 的迟到 $outcome 在 $boundary 后不通知或重新读取旧页面", async ({ page, boundary, outcome }) => {
  let release!: (value: ReturnType<typeof response>) => void
  const pending = new Promise<ReturnType<typeof response>>((resolve) => {
    release = resolve
  })
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return pending
    return response(url.endsWith("/configs") ? [] : { total: 0, items: [] })
  })
  vi.stubGlobal("fetch", fetch)
  const pinia = createPinia()
  const wrapper = mount(page === "blacklist" ? BlacklistView : SensitiveWordView, {
    attachTo: document.body,
    global: { plugins: [pinia, ElementPlus] },
  })
  await flushPromises()
  await wrapper.get(`[data-testid='${page}-add-open']`).trigger("click")
  await flushPromises()
  const input = document.querySelector(".el-drawer textarea") as HTMLTextAreaElement
  input.value = page === "blacklist" ? "13800138000" : "合成过期响应"
  input.dispatchEvent(new Event("input"))
  await flushPromises()
  ;(document.querySelector(`[data-testid='${page}-add']`) as HTMLButtonElement).click()
  await flushPromises()
  expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1)
  const success = vi.spyOn(ElMessage, "success")
  const failure = vi.spyOn(ElMessage, "error")
  if (boundary === "unmount") wrapper.unmount()
  else useSessionStore(pinia).clear()
  const reads = fetch.mock.calls.filter(([, init]) => init?.method !== "POST").length
  release(
    outcome === "success"
      ? response({ added: 1, skipped: 0, updated: 0, items: [] })
      : response({ code: "DEPENDENCY_UNAVAILABLE", message: "合成迟到失败" }, 503),
  )
  await flushPromises()
  expect(success).not.toHaveBeenCalled()
  expect(failure).not.toHaveBeenCalled()
  expect(fetch.mock.calls.filter(([, init]) => init?.method !== "POST")).toHaveLength(reads)
  if (boundary !== "unmount") wrapper.unmount()
})
