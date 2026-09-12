import { flushPromises } from "@vue/test-utils"
import { effectScope } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { ExportTask } from "../src/api/reports"
const api = vi.hoisted(() => ({ getExportTask: vi.fn(), issueExportStepUp: vi.fn(), downloadExport: vi.fn() }))
vi.mock("../src/api/reports", () => api)
import { useExportTask } from "../src/composables/useExportTask"
const task = (id: string, status: ExportTask["status"] = "running"): ExportTask => ({
  id,
  status,
  decrypted: false,
  row_count: null,
  download_url: null,
  expires_at: null,
  created_at: "2026-09-12T14:00:00+08:00",
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
describe("导出任务代际回归", () => {
  afterEach(() => {
    vi.useRealTimers()
    api.getExportTask.mockReset()
  })
  it.each(["done", "failed", "error"] as const)("旧轮询 %s 不覆盖新任务或停止新链", async (outcome) => {
    vi.useFakeTimers()
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true })
    const old = deferred<ExportTask>()
    api.getExportTask.mockReturnValueOnce(old.promise).mockResolvedValue(task("export-B"))
    const scope = effectScope()
    const controller = scope.run(() => useExportTask({ timeoutMessage: "timeout" }))!
    try {
      await controller.start(async () => task("export-A"))
      await controller.start(async () => task("export-B"))
      if (outcome === "error") old.reject(new Error("old error"))
      else old.resolve(task("export-A", outcome))
      await flushPromises()
      expect(controller.exportTask.value?.id).toBe("export-B")
      expect(controller.exportError.value).toBe("")
      await vi.advanceTimersByTimeAsync(2_000)
      expect(api.getExportTask).toHaveBeenLastCalledWith("export-B")
      expect(api.getExportTask).toHaveBeenCalledTimes(3)
    } finally {
      scope.stop()
    }
  })
  it.each(["success", "error", "dispose"])("创建乱序或卸载：%s", async (outcome) => {
    const old = deferred<ExportTask>()
    api.getExportTask.mockResolvedValue(task("B"))
    const scope = effectScope()
    const controller = scope.run(() => useExportTask({ timeoutMessage: "timeout" }))!
    const first = controller.start(() => old.promise)
    if (outcome === "dispose") scope.stop()
    else await controller.start(async () => task("B"))
    if (outcome === "error") old.reject(new Error("old creation"))
    else old.resolve(task("A"))
    expect(await first).toBe(false)
    expect(controller.exportTask.value?.id).toBe(outcome === "dispose" ? undefined : "B")
    expect(controller.exportError.value).toBe("")
    scope.stop()
  })
})
