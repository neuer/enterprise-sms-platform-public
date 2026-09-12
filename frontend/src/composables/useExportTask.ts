import { ElMessage, ElMessageBox } from "element-plus"
import { ref, type Ref } from "vue"

import { downloadExport, getExportTask, issueExportStepUp, type ExportTask } from "../api/reports"
import { saveBlob } from "../lib/download"
import { errorText } from "../lib/error"
import { useLatestRead } from "./useLatestRead"
import { usePolling } from "./usePolling"

export interface UseExportTaskOptions {
  /** 轮询兜底超时（150 次 × 2s ≈ 5 分钟）后的提示文案，各页面口径不同。 */
  timeoutMessage: string
}

export interface ExportTaskController {
  /** 当前导出任务快照；终态（done/failed）后轮询自动停止。 */
  exportTask: Ref<ExportTask | null>
  /** 创建 / 轮询 / 超时错误文案；成功路径为空串。 */
  exportError: Ref<string>
  /** 创建请求在途标记（按钮 loading）。 */
  exportBusy: Ref<boolean>
  /** 创建导出任务并重启轮询链（重复导出只保留一条链）；失败写入 exportError 并返回 false。 */
  start: (create: () => Promise<ExportTask>) => Promise<boolean>
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
export function useExportTask(options: UseExportTaskOptions): ExportTaskController {
  const exportTask = ref<ExportTask | null>(null)
  const exportError = ref("")
  const exportBusy = ref(false)
  const lifecycle = useLatestRead()
  let current: AbortSignal | undefined

  /** 查询一次导出任务状态；终态或查询失败返回 true 停止轮询。 */
  async function pollOnce(): Promise<boolean> {
    const signal = current
    const task = exportTask.value
    if (!task || !signal || signal.aborted) return true
    try {
      const result = await getExportTask(task.id)
      if (signal.aborted) return false
      exportTask.value = result
      return result.status === "done" || result.status === "failed"
    } catch (error) {
      if (signal.aborted) return false
      exportError.value = errorText(error, "导出状态查询失败")
      return true
    }
  }

  const polling = usePolling(pollOnce, {
    intervalMs: 2_000,
    maxAttempts: 150,
    onTimeout: () => {
      exportError.value = options.timeoutMessage
    },
  })

  async function start(create: () => Promise<ExportTask>): Promise<boolean> {
    const signal = lifecycle.start()
    current = signal
    polling.stop()
    exportTask.value = null
    exportBusy.value = true
    exportError.value = ""
    try {
      const result = await create()
      if (signal.aborted) return false
      exportTask.value = result
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
    if (!task) return
    let password: string | null = null
    try {
      let stepUpToken: string | undefined
      if (task.decrypted) {
        password = await promptExportPassword()
        if (password === null) return
        stepUpToken = (await issueExportStepUp(task.id, password)).token
      }
      const blob = await downloadExport(task, stepUpToken)
      saveBlob(blob, `${filenamePrefix}-${task.id}.csv`)
    } catch (error) {
      ElMessage.error(errorText(error, "下载失败"))
    } finally {
      password = null
    }
  }

  return { exportTask, exportError, exportBusy, start, download }
}
