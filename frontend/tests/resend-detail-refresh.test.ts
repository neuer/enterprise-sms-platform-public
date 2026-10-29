import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"
import BatchView from "../src/views/BatchView.vue"
import type { BatchItem } from "../src/api/queries"
import { useSessionStore } from "../src/stores/session"

vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }))

function response(body: unknown, status = 200) {
  return { ok: status < 400, status, headers: { get: () => null }, json: async () => body }
}

function deferred() {
  let resolve!: (value: ReturnType<typeof response>) => void
  const promise = new Promise<ReturnType<typeof response>>((done) => (resolve = done))
  return { promise, resolve }
}

const source = {
  batch_no: "source",
  category: "notice",
  channel: "web",
  creator: "synthetic",
  dept: "synthetic",
  content: "合成补发内容",
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
  scheduled_at: null,
  created_at: "2026-10-29T08:00:00+08:00",
  is_test: false,
  resend_of: null,
  deferred_reason: null,
  excluded_before_send: 0,
} as BatchItem

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it.each(
  ["ordinary", "supplement"].flatMap((action) =>
    ["success", "read-failure", "target", "session", "dispose"].map((outcome) => ({ action, outcome })),
  ),
)("$action 已受理后的详情刷新：$outcome", async ({ action, outcome }) => {
  const initial = {
    ...source,
    can_resend_failed: action === "ordinary",
    resend_block_reason: action === "supplement" ? ("supplement_required" as const) : null,
    resend_covered_failed: action === "supplement" ? 1 : 0,
    resend_uncovered_failed: action === "supplement" ? 1 : 2,
  }
  const authoritative = {
    ...source,
    can_resend_failed: true,
    resend_block_reason: null,
    resend_child_batch_no: "child",
    resend_covered_failed: 2,
    resend_uncovered_failed: 0,
  }
  const read = deferred()
  let detailReads = 0
  let listReads = 0
  let writes = 0
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === "POST") {
        if (url.endsWith("/resend-supplement"))
          return response({ intent_id: "40000000-0000-4000-8000-000000000882", covered_failed: 1, uncovered_failed: 1 })
        writes += 1
        return response({ batch_no: "child", resend_of: "source", accepted: 1, status: "queued", idempotent: false })
      }
      if (url.endsWith("/admin/apps")) return response([])
      if (url.endsWith("/batches/source")) {
        detailReads += 1
        return detailReads === 1 ? read.promise : response(authoritative)
      }
      if (url.includes("/details?")) return response({ items: [], total: 0 })
      listReads += 1
      return response({ items: [initial], total: 1, status_counts: {} })
    }),
  )
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const success = vi.spyOn(ElMessage, "success")
  const error = vi.spyOn(ElMessage, "error")
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "operator"
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as {
    selected: BatchItem
    drawer: boolean
    detailUnconfirmed: boolean
    canResendFailed: boolean
    resendFailed: () => Promise<void>
    supplementUncovered: () => Promise<void>
  }
  vm.selected = initial
  vm.drawer = true
  const running = action === "ordinary" ? vm.resendFailed() : vm.supplementUncovered()
  await flushPromises()
  expect(writes).toBe(1)
  expect(detailReads).toBe(1)
  expect(success).toHaveBeenCalledTimes(1)
  const listsBefore = listReads
  if (outcome === "target") vm.selected = { ...source, batch_no: "replacement" }
  if (outcome === "session") useSessionStore().clear()
  if (outcome === "dispose") wrapper.unmount()
  read.resolve(
    outcome === "read-failure"
      ? response({ code: "INTERNAL_ERROR", message: "详情读取暂时失败", detail: null }, 500)
      : response(authoritative),
  )
  await running
  await flushPromises()
  expect(error).not.toHaveBeenCalled()
  expect(writes).toBe(1)
  if (outcome === "success") {
    expect(vm.selected.resend_uncovered_failed).toBe(0)
    expect(wrapper.find('[data-testid="resend-supplement"]').exists()).toBe(false)
    expect(listReads).toBeGreaterThan(listsBefore)
  } else if (outcome === "read-failure") {
    expect(vm.detailUnconfirmed).toBe(true)
    expect(vm.canResendFailed).toBe(false)
    expect(wrapper.get('[data-testid="resend-failed"]').attributes("disabled")).toBeDefined()
    if (action === "supplement") {
      expect(wrapper.get('[data-testid="resend-supplement"]').attributes("disabled")).toBeDefined()
      await vm.supplementUncovered()
      expect(writes).toBe(1)
    }
    await wrapper.get('[data-testid="retry-batch-detail"]').trigger("click")
    await flushPromises()
    expect(vm.detailUnconfirmed).toBe(false)
    expect(vm.selected.resend_uncovered_failed).toBe(0)
    expect(writes).toBe(1)
  } else {
    expect(listReads).toBe(listsBefore)
    if (outcome === "target") expect(vm.selected.batch_no).toBe("replacement")
    else expect(vm.selected.resend_uncovered_failed).toBe(initial.resend_uncovered_failed)
  }
  if (outcome !== "dispose") wrapper.unmount()
})
