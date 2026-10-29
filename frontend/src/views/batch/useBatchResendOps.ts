import { ElMessage } from "element-plus"
import { ref, type ComputedRef, type Ref } from "vue"
import { confirmResendSupplement, previewResendSupplement, resendFailedBatch, type BatchItem } from "../../api/queries"
import { useConfirmActions } from "../../lib/confirm"
import { errorText } from "../../lib/error"
import { STATUS_LABELS } from "../../lib/labels"

/** 普通重发和未覆盖补发：固定操作对象，旧写响应不影响新的页面或详情。 */
export function useBatchResendOps(deps: {
  selected: Ref<BatchItem | null>
  drawer: Ref<boolean>
  canResendFailed: ComputedRef<boolean>
  detailUnconfirmed: Ref<boolean>
  openToken: () => number
  refreshSelected: () => Promise<void>
}) {
  const { confirmAuditedAction, captureCurrent } = useConfirmActions()
  const resending = ref(false)
  const resendChild = ref<{ parent: string; batchNo: string; status: string } | null>(null)
  /** 已发出的请求可以提交，但旧页面或旧详情不得接收其通知和展示结果。 */
  function captureResendDetail(batch: string): () => boolean {
    const isCurrent = captureCurrent()
    const token = deps.openToken()
    return () =>
      isCurrent() && token === deps.openToken() && deps.drawer.value && deps.selected.value?.batch_no === batch
  }
  async function resendFailed(): Promise<void> {
    if (!deps.selected.value || !deps.canResendFailed.value || resending.value) return
    const target = deps.selected.value
    const batch = target.batch_no
    const isCurrent = captureResendDetail(batch)
    resending.value = true
    try {
      if (
        !(await confirmAuditedAction({
          isCurrent: () => deps.selected.value === target && deps.canResendFailed.value,
          title: "确认重发",
          body: "失败号码将生成新批次并完整重走频控、审批和时间窗。",
          auditNote: "重发行为与操作人将写入审计日志。",
          confirmText: "确认重发",
        }))
      )
        return
      const result = await resendFailedBatch(batch)
      if (!isCurrent()) return
      resendChild.value = { parent: batch, batchNo: result.batch_no, status: result.status }
      const statusLabel = STATUS_LABELS[result.status] ?? result.status
      ElMessage.success(
        result.idempotent
          ? `重发批次 ${result.batch_no} 已存在，当前状态：${statusLabel}`
          : `重发批次 ${result.batch_no} 已创建，当前状态：${statusLabel} · 本次操作已记入审计`,
      )
      await deps.refreshSelected()
    } catch (error) {
      if (!isCurrent()) return
      ElMessage.error(errorText(error, "重发失败"))
    } finally {
      resending.value = false
    }
  }

  async function supplementUncovered(): Promise<void> {
    const batch = deps.selected.value
    if (
      !batch ||
      resending.value ||
      deps.detailUnconfirmed.value ||
      batch.resend_block_reason !== "supplement_required"
    )
      return
    const isCurrent = captureResendDetail(batch.batch_no)
    resending.value = true
    try {
      const preview = await previewResendSupplement(batch.batch_no)
      if (!isCurrent()) return
      if (
        !(await confirmAuditedAction({
          isCurrent: () => deps.selected.value === batch && batch.resend_block_reason === "supplement_required",
          title: "确认补发未覆盖号码",
          body: `已覆盖 ${preview.covered_failed} 条，本次只补发未覆盖的 ${preview.uncovered_failed} 条。已覆盖号码不会再次发送。`,
          auditNote: "补发行为、操作人与原批次将写入审计日志。",
          confirmText: "补发未覆盖",
        }))
      )
        return
      const result = await confirmResendSupplement(batch.batch_no, preview.intent_id)
      if (!isCurrent()) return
      resendChild.value = { parent: batch.batch_no, batchNo: result.batch_no, status: result.status }
      ElMessage.success("未覆盖号码已进入新批次 · 本次操作已记入审计")
      await deps.refreshSelected()
    } catch (error) {
      if (!isCurrent()) return
      ElMessage.error(errorText(error, "补发未覆盖号码失败"))
    } finally {
      resending.value = false
    }
  }

  return { resending, resendChild, resendFailed, supplementUncovered }
}
