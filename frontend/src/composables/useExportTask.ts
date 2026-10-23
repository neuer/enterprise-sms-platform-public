import { defaultSessionDocument } from "../api/sessionDocument"
import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"
import { ElMessage, ElMessageBox } from "element-plus"
import { watch, getCurrentScope, onScopeDispose, ref, type Ref } from "vue"

import { ApiRequestError } from "../api/client"
import { downloadExport, getExportTask, issueExportStepUp, type ExportTask } from "../api/reports"
import { saveBlob } from "../lib/download"
import { errorText } from "../lib/error"
import { useLatestRead } from "./useLatestRead"
import { usePolling } from "./usePolling"

export type ExportTracking = "idle" | "tracking" | "paused" | "terminal"

export interface UseExportTaskOptions {
  /** 轮询兜底超时（150 次 × 2s ≈ 5 分钟）后的提示文案，各页面口径不同。 */
  timeoutMessage: string
}

export interface ExportTaskController {
  /** 当前导出任务快照；查询失败不清空，终态后轮询停止。 */
  exportTask: Ref<ExportTask | null>
  /** 创建 / 轮询 / 超时错误文案；成功查询后清空。 */
  exportError: Ref<string>
  /** 同一任务的跟踪阶段。paused 表示可重新查询，不代表后台任务失败。 */
  exportTracking: Ref<ExportTracking>
  /** 创建请求在途标记（按钮 loading）。 */
  exportBusy: Ref<boolean>
  /** 创建导出任务并重启轮询链（重复导出只保留一条链）；失败写入 exportError 并返回 false。 */
  start: (create: () => Promise<ExportTask>) => Promise<boolean>
  /** 对仍存在的同一任务重新开始只读跟踪；在途调用去重，不创建新任务。 */
  retryTracking: () => Promise<void>
  /**
   * 下载导出文件为 `{filenamePrefix}-{任务id}.csv`：明文导出先弹密码框换取
   * step-up 单次令牌（密码只存局部易失变量，用完即清）；取消 / 关闭密码框安静退出。
   */
  download: (filenamePrefix: string) => Promise<void>
}

/** 明文导出 step-up 密码框：确认返回密码，取消 / 关闭返回 null（不视为错误）。 */
async function promptExportPassword(): Promise<string | null> {
  try {
    const prompt = await ElMessageBox.prompt("明文导出属于高风险操作，请重新输入当前认证源密码。", "下载明文导出", {
      inputType: "password",
      inputPlaceholder: "当前密码",
      confirmButtonText: "验证并下载",
      cancelButtonText: "取消",
    })
    return prompt.value
  } catch {
    return null
  }
}

/**
 * 导出任务流单点：创建 → 2s 轮询（终态自停、150 次兜底超时）→ step-up 下载。
 * 报表明细导出与运维无主报告导出共用；页面只提供创建请求与文件名片段。
 */
/** 可重试的状态查询：网络、超时和 5xx 不把后台任务判成失败。 */
function exportQueryKind(error: unknown): "cancel" | "auth" | "terminal" | "retry" {
  if (error instanceof DOMException && error.name === "AbortError") return "cancel"
  if (!(error instanceof ApiRequestError)) return "retry"
  if (error.status === 401) return "auth"
  if (error.status === 403 || error.status === 404 || error.status === 410) return "terminal"
  return "retry"
}

function terminalQueryMessage(error: ApiRequestError): string {
  if (error.status === 403) return "没有权限继续查询该导出任务。"
  if (error.status === 404 || error.status === 410) return "导出任务不存在或已过期，无法继续跟踪。"
  return "无法继续查询该导出任务。"
}

export function useExportTask(options: UseExportTaskOptions): ExportTaskController {
  const exportTask = ref<ExportTask | null>(null)
  const exportError = ref("")
  const exportTracking = ref<ExportTracking>("idle")
  const exportBusy = ref(false)
  const lifecycle = useLatestRead()
  let current: AbortSignal | undefined
  let disposed = false
  let failureStreak = 0
  let resumeFlight: Promise<void> | null = null
  let downloadController: AbortController | undefined
  watch(
    () => exportTask.value?.id,
    () => downloadController?.abort(),
    { flush: "sync" },
  )

  function invalidate(): void {
    lifecycle.cancel()
    downloadController?.abort()
    polling.stop()
    exportTask.value = null
    exportBusy.value = false
    exportError.value = ""
    exportTracking.value = "idle"
    failureStreak = 0
  }
  function handleOnline(): void {
    if (exportTracking.value !== "paused") return
    void retryTracking()
  }
  window.addEventListener(SESSION_CLEARING_EVENT, invalidate)
  window.addEventListener("online", handleOnline)
  if (getCurrentScope())
    onScopeDispose(() => {
      disposed = true
      invalidate()
      window.removeEventListener(SESSION_CLEARING_EVENT, invalidate)
      window.removeEventListener("online", handleOnline)
    })

  /**
   * 查询一次导出任务状态。
   * 终态、鉴权失效和不可恢复错误停止跟踪；网络类错误保留任务并有界退避。
   */
  async function pollOnce(): Promise<boolean> {
    const signal = current
    const taskId = exportTask.value?.id
    if (!taskId || !signal || signal.aborted || disposed) return true
    try {
      const result = await getExportTask(taskId, signal)
      if (signal.aborted || disposed || exportTask.value?.id !== taskId) return false
      failureStreak = 0
      exportTask.value = result
      exportError.value = ""
      if (result.status === "done" || result.status === "failed") {
        exportTracking.value = "terminal"
        if (result.status === "failed") exportError.value = "导出生成失败。请处理后重新导出。"
        return true
      }
      exportTracking.value = "tracking"
      return false
    } catch (error) {
      if (signal.aborted || disposed || exportTask.value?.id !== taskId) return false
      const kind = exportQueryKind(error)
      if (kind === "cancel" || kind === "auth") {
        exportTracking.value = "terminal"
        if (kind === "auth") exportError.value = "登录状态已失效，导出跟踪已停止。"
        return true
      }
      if (kind === "terminal" && error instanceof ApiRequestError) {
        exportTracking.value = "terminal"
        exportError.value = terminalQueryMessage(error)
        return true
      }
      failureStreak += 1
      exportTracking.value = "tracking"
      exportError.value = "导出状态暂时查询失败，将自动重试同一任务。"
      const waitMs = Math.min(8_000, 250 * 2 ** Math.min(failureStreak, 5))
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, waitMs)
      })
      return signal.aborted || disposed || exportTask.value?.id !== taskId
    }
  }

  const polling = usePolling(pollOnce, {
    intervalMs: 2_000,
    maxAttempts: 150,
    onTimeout: () => {
      if (exportTracking.value === "terminal") return
      exportTracking.value = "paused"
      exportError.value = options.timeoutMessage
    },
  })

  /** 重新查询当前任务。已在跟踪时不另开轮询链。 */
  function retryTracking(): Promise<void> {
    if (resumeFlight) return resumeFlight
    const run = async (): Promise<void> => {
      if (disposed || !exportTask.value || exportTracking.value === "terminal" || exportTracking.value === "tracking")
        return
      failureStreak = 0
      exportError.value = ""
      exportTracking.value = "tracking"
      polling.restart()
    }
    resumeFlight = run().finally(() => {
      resumeFlight = null
    })
    return resumeFlight
  }

  async function start(create: () => Promise<ExportTask>): Promise<boolean> {
    if (disposed) return false
    downloadController?.abort()
    const signal = lifecycle.start()
    current = signal
    polling.stop()
    exportTask.value = null
    exportBusy.value = true
    exportError.value = ""
    exportTracking.value = "idle"
    failureStreak = 0
    try {
      const result = await create()
      if (signal.aborted) return false
      exportTask.value = result
      failureStreak = 0
      exportError.value = ""
      exportTracking.value = result.status === "done" || result.status === "failed" ? "terminal" : "tracking"
      // 重复发起导出会开启新任务：restart 重置旧轮询链，保证任何时候只有一条。
      polling.restart()
      return true
    } catch (error) {
      if (signal.aborted) return false
      exportError.value = errorText(error, "导出创建失败")
      return false
    } finally {
      if (!signal.aborted) exportBusy.value = false
    }
  }

  async function download(filenamePrefix: string): Promise<void> {
    const task = exportTask.value
    if (!task || disposed) return
    const origin = defaultSessionDocument.captureOrigin()
    downloadController?.abort()
    const controller = new AbortController()
    downloadController = controller
    const release = defaultSessionDocument.trackController(controller)
    const live = () =>
      !disposed &&
      !controller.signal.aborted &&
      defaultSessionDocument.isOriginCurrent(origin) &&
      exportTask.value?.id === task.id
    let password: string | null = null
    let stepUpToken: string | undefined
    try {
      if (task.decrypted) {
        password = await promptExportPassword()
        if (password === null || !live()) return
        stepUpToken = (await issueExportStepUp(task.id, password, controller.signal)).token
        password = null
        if (!live()) return
      }
      const blob = await downloadExport(task, stepUpToken, controller.signal)
      if (!live()) return
      saveBlob(blob, `${filenamePrefix}-${task.id}.csv`)
    } catch (error) {
      if (live()) ElMessage.error(errorText(error, "下载失败"))
    } finally {
      password = null
      stepUpToken = undefined
      release()
    }
  }

  return { exportTask, exportError, exportTracking, exportBusy, start, retryTracking, download }
}
