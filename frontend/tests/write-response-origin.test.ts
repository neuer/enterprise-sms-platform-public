import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"
import BatchView from "../src/views/BatchView.vue"
import CallbackView from "../src/views/CallbackView.vue"
import BlacklistView from "../src/views/BlacklistView.vue"
import SensitiveWordView from "../src/views/SensitiveWordView.vue"
import { useSessionStore } from "../src/stores/session"
vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }))
function response(body: unknown, status = 200) {
  return { ok: status < 400, status, headers: { get: () => null }, json: async () => body }
}
function deferred() {
  let resolve!: (value: ReturnType<typeof response>) => void
  const promise = new Promise<ReturnType<typeof response>>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
const batch = {
  batch_no: "origin",
  category: "notice",
  channel: "web",
  app_name: null,
  creator: "test",
  dept: "synthetic",
  content: "合成内容",
  status: "completed",
  total: 2,
  failed: 2,
  pending: 0,
  sent: 0,
  delivered: 0,
  unknown: 0,
  other: 0,
  segments: 1,
  quota_cost: 2,
  removed_freq_limit: 0,
  scheduled_at: "2026-10-31T00:00:00Z",
  created_at: "2026-10-28T00:00:00Z",
  is_test: false,
  resend_of: null,
  deferred_reason: null,
}
const accepted = { batch_no: "child", resend_of: "origin", accepted: 2, status: "queued", idempotent: false }
const preview = {
  intent_id: "40000000-0000-4000-8000-000000000882",
  covered_failed: 1,
  uncovered_failed: 1,
  phone_masks: [],
}
const error = { code: "SERVICE_UNAVAILABLE", message: "合成写请求失败", detail: null }
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const batchActions = [
  "cancelSelected",
  "saveReschedule",
  "resendFailed",
  "supplementPreview",
  "supplementConfirm",
] as const
it.each(
  batchActions.flatMap((action) =>
    ["dispose", "session", "target", "current"].flatMap((change) =>
      [false, true].map((failure) => ({ action, change, failure })),
    ),
  ),
)("批次 $action 写响应在 $change 后隔离，失败=$failure", async ({ action, change, failure }) => {
  const pending = deferred()
  const posts: string[] = []
  let reads = 0
  const isSupplement = action.startsWith("supplement")
  const original = {
    ...batch,
    status: action === "cancelSelected" || action === "saveReschedule" ? "scheduled" : "completed",
    resend_block_reason: isSupplement ? "supplement_required" : null,
  }
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === "POST") {
        posts.push(url)
        if (url.endsWith("/resend-supplement"))
          return action === "supplementPreview" ? pending.promise : response(preview)
        if (action === "supplementPreview") return response(accepted)
        return pending.promise
      }
      reads += 1
      if (url.endsWith("/admin/apps")) return response([])
      if (url.includes("/details?")) return response({ total: 0, items: [] })
      if (url.endsWith("/batches/origin")) return response(original)
      return response({ total: 1, items: [original], status_counts: {} })
    }),
  )
  const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const success = vi.spyOn(ElMessage, "success")
  const failureToast = vi.spyOn(ElMessage, "error")
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "admin"
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as {
    selected: typeof original
    drawer: boolean
    scheduledAt: string
    cancelSelected: () => Promise<void>
    saveReschedule: () => Promise<void>
    resendFailed: () => Promise<void>
    supplementUncovered: () => Promise<void>
  }
  vm.selected = original
  vm.drawer = true
  vm.scheduledAt = batch.scheduled_at
  const writing = isSupplement
    ? vm.supplementUncovered()
    : vm[action as "cancelSelected" | "saveReschedule" | "resendFailed"]()
  await flushPromises()
  expect(posts.length).toBe(action === "supplementConfirm" ? 2 : 1)
  const readsBefore = reads
  if (change === "dispose") wrapper.unmount()
  if (change === "session") {
    useSessionStore().clear()
    useSessionStore().role = "admin"
  }
  if (change === "target") vm.selected = { ...original, batch_no: "replacement" }
  pending.resolve(failure ? response(error, 503) : response(action === "supplementPreview" ? preview : accepted))
  await writing
  await flushPromises()
  if (change !== "current") {
    expect(success).not.toHaveBeenCalled()
    expect(failureToast).not.toHaveBeenCalled()
    expect(reads).toBe(readsBefore)
    if (change === "target") {
      expect(vm.drawer).toBe(true)
      expect(vm.selected.batch_no).toBe("replacement")
    }
    if (action === "supplementPreview") expect(confirm).not.toHaveBeenCalled()
  } else {
    expect(failure ? failureToast : success).toHaveBeenCalledTimes(1)
    if (!failure) expect(reads).toBeGreaterThan(readsBefore)
  }
  if (change !== "dispose") wrapper.unmount()
})
const mutationCases = [
  {
    name: "callback retry",
    component: CallbackView,
    method: "retry",
    item: { id: 9, app_name: "synthetic", event: "batch.finished" },
  },
  {
    name: "callback recovery",
    component: CallbackView,
    method: "recover",
    item: { id: 9, app_name: "synthetic", event: "batch.finished" },
  },
  {
    name: "blacklist delete",
    component: BlacklistView,
    method: "remove",
    item: { phone_hmac: "a".repeat(64), phone_mask: "138****8000" },
  },
  { name: "sensitive delete", component: SensitiveWordView, method: "remove", item: { id: 9, word: "合成词" } },
] as const
it.each(
  mutationCases.flatMap((test) =>
    ["dispose", "session", "current"].flatMap((change) =>
      [false, true].map((failure) => ({ ...test, change, failure })),
    ),
  ),
)("$name 的写响应在 $change 后隔离，失败=$failure", async ({ component, method, item, change, failure }) => {
  const pending = deferred()
  let reads = 0
  let writes = 0
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      if (init?.method === "POST" || init?.method === "DELETE") {
        writes += 1
        return pending.promise
      }
      reads += 1
      if (String(input).endsWith("/admin/apps") || String(input).endsWith("/admin/configs")) return response([])
      return response({ total: 0, items: [], dead_total: 0 })
    }),
  )
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const success = vi.spyOn(ElMessage, "success")
  const failureToast = vi.spyOn(ElMessage, "error")
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "admin"
  const wrapper = mount(component, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  success.mockClear()
  failureToast.mockClear()
  const vm = wrapper.vm as unknown as Record<string, (item: unknown) => Promise<void>>
  const writing = vm[method](item)
  await flushPromises()
  expect(writes).toBe(1)
  const readsBefore = reads
  if (change === "dispose") wrapper.unmount()
  if (change === "session") {
    useSessionStore().clear()
    useSessionStore().role = "admin"
  }
  pending.resolve(failure ? response(error, 503) : response({ task_id: 10, created: true }))
  await writing
  await flushPromises()
  if (change !== "current") {
    expect(success).not.toHaveBeenCalled()
    expect(failureToast).not.toHaveBeenCalled()
    expect(reads).toBe(readsBefore)
  } else {
    expect(failure ? failureToast : success).toHaveBeenCalledTimes(1)
    if (!failure) expect(reads).toBeGreaterThan(readsBefore)
  }
  if (change !== "dispose") wrapper.unmount()
})
