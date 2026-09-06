import { ElMessage, ElMessageBox } from "element-plus"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { ExportTask } from "../src/api/reports"

const reportsApi = vi.hoisted(() => ({
  getExportTask: vi.fn(),
  issueExportStepUp: vi.fn(),
  downloadExport: vi.fn(),
}))
vi.mock("../src/api/reports", () => reportsApi)

const downloadLib = vi.hoisted(() => ({ saveBlob: vi.fn() }))
vi.mock("../src/lib/download", () => downloadLib)

import { useExportTask } from "../src/composables/useExportTask"

const TIMEOUT_MESSAGE = "导出状态查询超时（已超过 5 分钟），请稍后重新发起导出"

function task(overrides: Partial<ExportTask> = {}): ExportTask {
  return {
    id: "c0a80101-0000-4000-8000-000000000134",
    status: "pending",
    decrypted: false,
    row_count: null,
    download_url: null,
    expires_at: null,
    created_at: "2026-07-12T08:00:00+08:00",
    ...overrides,
  }
}

function mountController() {
  return useExportTask({ timeoutMessage: TIMEOUT_MESSAGE })
}

describe("导出任务流 useExportTask", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    reportsApi.getExportTask.mockReset()
    reportsApi.issueExportStepUp.mockReset()
    reportsApi.downloadExport.mockReset()
    downloadLib.saveBlob.mockReset()
  })

  it("创建后轮询至 done 终态自停，行数随任务快照更新", async () => {
    reportsApi.getExportTask
      .mockResolvedValueOnce(task({ status: "running" }))
      .mockResolvedValueOnce(task({ status: "done", row_count: 15, download_url: "/download" }))
    const controller = mountController()

    const created = await controller.start(() => Promise.resolve(task()))
    expect(created).toBe(true)
    expect(controller.exportBusy.value).toBe(false)
    // restart 立即执行第一次查询
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(1)
    expect(controller.exportTask.value?.status).toBe("running")

    await vi.advanceTimersByTimeAsync(2_000)
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(2)
    expect(controller.exportTask.value?.status).toBe("done")
    expect(controller.exportTask.value?.row_count).toBe(15)

    // 终态自停：不再发起查询
    await vi.advanceTimersByTimeAsync(10_000)
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(2)
    expect(controller.exportError.value).toBe("")
  })

  it("重复 start 只保留一条轮询链", async () => {
    reportsApi.getExportTask.mockResolvedValue(task())
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await controller.start(() => Promise.resolve(task()))
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(2)

    // 若旧链未清理，每个 2s 周期会出现两次查询
    await vi.advanceTimersByTimeAsync(2_000)
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(3)
  })

  it("创建失败写入 exportError 并返回 false，不启动轮询", async () => {
    const controller = mountController()
    const created = await controller.start(() => Promise.reject(new Error("配额不足")))
    expect(created).toBe(false)
    expect(controller.exportError.value).toBe("配额不足")
    expect(controller.exportBusy.value).toBe(false)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(reportsApi.getExportTask).not.toHaveBeenCalled()
  })

  it("状态查询失败写入 exportError 并停止轮询", async () => {
    reportsApi.getExportTask.mockRejectedValueOnce(new Error("网络异常"))
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    expect(controller.exportError.value).toBe("网络异常")
    await vi.advanceTimersByTimeAsync(5_000)
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(1)
  })

  it("150 次未完成触发页面口径的超时提示", async () => {
    reportsApi.getExportTask.mockResolvedValue(task())
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await vi.advanceTimersByTimeAsync(149 * 2_000)
    expect(controller.exportError.value).toBe(TIMEOUT_MESSAGE)
    const calls = reportsApi.getExportTask.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(reportsApi.getExportTask).toHaveBeenCalledTimes(calls)
  })

  it("掩码导出直接下载为 {前缀}-{任务id}.csv", async () => {
    const done = task({ status: "done", download_url: "/download" })
    reportsApi.getExportTask.mockResolvedValue(done)
    reportsApi.downloadExport.mockResolvedValue(new Blob(["csv"]))
    const prompt = vi.spyOn(ElMessageBox, "prompt")
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await controller.download("sms-report")

    expect(prompt).not.toHaveBeenCalled()
    expect(reportsApi.issueExportStepUp).not.toHaveBeenCalled()
    expect(reportsApi.downloadExport).toHaveBeenCalledWith(controller.exportTask.value, undefined)
    expect(downloadLib.saveBlob).toHaveBeenCalledTimes(1)
    expect(downloadLib.saveBlob.mock.calls[0][1]).toBe("sms-report-c0a80101-0000-4000-8000-000000000134.csv")
  })

  it("明文导出先弹密码框换 step-up 单次令牌再下载", async () => {
    const done = task({ status: "done", decrypted: true, download_url: "/download" })
    reportsApi.getExportTask.mockResolvedValue(done)
    reportsApi.issueExportStepUp.mockResolvedValue({ token: "single-use-token", expires_in: 300 })
    reportsApi.downloadExport.mockResolvedValue(new Blob(["csv"]))
    vi.spyOn(ElMessageBox, "prompt").mockResolvedValue({ value: "current-password", action: "confirm" } as never)
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await controller.download("sms-report")

    expect(reportsApi.issueExportStepUp).toHaveBeenCalledWith(done.id, "current-password")
    expect(reportsApi.downloadExport).toHaveBeenCalledWith(controller.exportTask.value, "single-use-token")
    expect(downloadLib.saveBlob).toHaveBeenCalledTimes(1)
  })

  it("取消密码框安静退出：不下载、不报错", async () => {
    const done = task({ status: "done", decrypted: true, download_url: "/download" })
    reportsApi.getExportTask.mockResolvedValue(done)
    vi.spyOn(ElMessageBox, "prompt").mockRejectedValue("cancel" as never)
    const error = vi.spyOn(ElMessage, "error")
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await controller.download("sms-report")

    expect(reportsApi.issueExportStepUp).not.toHaveBeenCalled()
    expect(reportsApi.downloadExport).not.toHaveBeenCalled()
    expect(downloadLib.saveBlob).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })

  it("下载失败弹出统一错误文案", async () => {
    const done = task({ status: "done", download_url: "/download" })
    reportsApi.getExportTask.mockResolvedValue(done)
    reportsApi.downloadExport.mockRejectedValue(new Error("导出文件尚未就绪"))
    const error = vi.spyOn(ElMessage, "error")
    const controller = mountController()
    await controller.start(() => Promise.resolve(task()))
    await controller.download("sms-report")
    expect(error).toHaveBeenCalledWith("导出文件尚未就绪")
  })

  it("无任务时 download 为空操作", async () => {
    const controller = mountController()
    await controller.download("sms-report")
    expect(reportsApi.downloadExport).not.toHaveBeenCalled()
  })
})
