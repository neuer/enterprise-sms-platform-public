import { ElMessage } from "element-plus"
import { computed, ref, type ComputedRef, type Ref } from "vue"

import type { BatchItem } from "../../api/queries"
import { errorText } from "../../lib/error"
import { toApiDateTime } from "../../lib/time"
import { bindBatchConflict, type BatchConflictBind } from "./stateConflict"

type ConfirmOptions = {
  isCurrent?: () => boolean
  title: string
  body: string
  auditNote: string
  confirmText?: string
}

/** 批次取消/改期：确认后写请求，409 只刷新权威状态，不自动重试写入。 */
export function useBatchScheduleOps(deps: {
  selected: Ref<BatchItem | null>
  drawer: Ref<boolean>
  canWrite: ComputedRef<boolean>
  openToken: () => number
  startRead: () => AbortSignal
  getBatch: BatchConflictBind["getBatch"]
  reloadList: () => Promise<void>
  confirmAuditedAction: (options: ConfirmOptions) => Promise<boolean>
  cancelBatch: (batchNo: string) => Promise<unknown>
  rescheduleBatch: (batchNo: string, scheduledAt: string) => Promise<unknown>
}) {
  const detailUnconfirmed = ref(false)
  const detailRefreshPending = ref(false)
  const rescheduleOpen = ref(false)
  const scheduledAt = ref("")
  const rescheduling = ref(false)
  const cancelling = ref(false)
  const conflict = bindBatchConflict({
    token: deps.openToken,
    drawer: deps.drawer,
    selected: deps.selected,
    startRead: deps.startRead,
    rescheduleOpen,
    unconfirmed: detailUnconfirmed,
    pending: detailRefreshPending,
    getBatch: deps.getBatch,
    reloadList: deps.reloadList,
  })
  const canScheduleOps = computed(
    () =>
      deps.canWrite.value &&
      !detailUnconfirmed.value &&
      !detailRefreshPending.value &&
      deps.selected.value?.status === "scheduled",
  )

  function resetDetailFlags(): void {
    detailUnconfirmed.value = false
    detailRefreshPending.value = false
  }

  async function cancelSelected(): Promise<void> {
    if (!deps.selected.value || !canScheduleOps.value || cancelling.value) return
    const target = deps.selected.value
    const batch = target.batch_no
    cancelling.value = true
    try {
      if (
        !(await deps.confirmAuditedAction({
          isCurrent: () => deps.selected.value === target && canScheduleOps.value,
          title: "确认取消",
          body: `取消批次 ${batch}？配额将按规则回补。`,
          auditNote: "取消行为与操作人将写入审计日志。",
          confirmText: "确认取消",
        }))
      )
        return
      await deps.cancelBatch(batch)
      deps.drawer.value = false
      ElMessage.success("批次已取消 · 本次操作已记入审计")
      await deps.reloadList()
    } catch (error) {
      if (await conflict.recoverIfConflict(error, batch)) return
      ElMessage.error(errorText(error, "取消失败"))
    } finally {
      cancelling.value = false
    }
  }

  function openReschedule(): void {
    if (!canScheduleOps.value) return
    scheduledAt.value = deps.selected.value?.scheduled_at || ""
    rescheduleOpen.value = true
  }

  async function saveReschedule(): Promise<void> {
    if (!deps.selected.value || !scheduledAt.value || rescheduling.value) return
    const target = deps.selected.value
    const batch = target.batch_no
    if (
      !(await deps.confirmAuditedAction({
        isCurrent: () => deps.selected.value === target && canScheduleOps.value,
        title: "确认改期",
        body: `批次 ${batch} 将改到新的定时时间，并重新执行审批判定；到达新时间前批次保持「已排期」。`,
        auditNote: "改期行为与操作人将写入审计日志。",
        confirmText: "确认改期",
      }))
    )
      return
    rescheduling.value = true
    try {
      await deps.rescheduleBatch(batch, toApiDateTime(new Date(scheduledAt.value)))
      rescheduleOpen.value = false
      deps.drawer.value = false
      ElMessage.success("批次已改期并重新执行审批判定 · 本次操作已记入审计")
      await deps.reloadList()
    } catch (error) {
      if (await conflict.recoverIfConflict(error, batch)) return
      ElMessage.error(errorText(error, "改期失败"))
    } finally {
      rescheduling.value = false
    }
  }

  return {
    detailUnconfirmed,
    canScheduleOps,
    cancelling,
    rescheduling,
    rescheduleOpen,
    scheduledAt,
    resetDetailFlags,
    cancelSelected,
    openReschedule,
    saveReschedule,
    retrySelectedRead: conflict.retry,
  }
}
