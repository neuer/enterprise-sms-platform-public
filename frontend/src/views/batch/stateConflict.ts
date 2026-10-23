import { ElMessage } from "element-plus"

import { ApiRequestError } from "../../api/client"
import type { BatchItem } from "../../api/queries"

/** 409 / STATE_CONFLICT：状态已被其他路径改变，调用方不得自动重试写请求。 */
export function isStateConflict(error: unknown): boolean {
  return error instanceof ApiRequestError && (error.code === "STATE_CONFLICT" || error.status === 409)
}

export interface BatchConflictHost {
  openToken: () => number
  drawerOpen: () => boolean
  selectedBatchNo: () => string | undefined
  startRead: () => AbortSignal
  getBatch: (batchNo: string, signal: AbortSignal) => Promise<BatchItem>
  applyBatch: (batch: BatchItem) => void
  setUnconfirmed: (value: boolean) => void
  setPending: (value: boolean) => void
  closeReschedule: () => void
  reloadList: () => Promise<void>
}

function stillSelected(host: BatchConflictHost, batchNo: string, tokenAtStart: number, signal: AbortSignal): boolean {
  return !signal.aborted && tokenAtStart === host.openToken() && host.drawerOpen() && host.selectedBatchNo() === batchNo
}

/** 按最初批次号重读详情和列表。迟到结果只在原抽屉、原批次仍打开时写回。 */
export async function refreshBatchSelection(
  host: BatchConflictHost,
  batchNo: string,
  tokenAtStart: number,
): Promise<void> {
  const signal = host.startRead()
  try {
    const batch = await host.getBatch(batchNo, signal)
    if (!stillSelected(host, batchNo, tokenAtStart, signal)) return
    host.setUnconfirmed(false)
    host.applyBatch(batch)
  } catch {
    if (!stillSelected(host, batchNo, tokenAtStart, signal)) return
    host.setUnconfirmed(true)
    return
  }
  if (signal.aborted || tokenAtStart !== host.openToken()) return
  await host.reloadList()
}

export interface BatchConflictBind {
  token: () => number
  drawer: { value: boolean }
  selected: { value: BatchItem | null }
  startRead: () => AbortSignal
  rescheduleOpen: { value: boolean }
  unconfirmed: { value: boolean }
  pending: { value: boolean }
  getBatch: (batchNo: string, signal: AbortSignal) => Promise<BatchItem>
  reloadList: () => Promise<void>
}

/** 把抽屉引用收成冲突恢复动作，避免页面继续堆状态机。 */
export function bindBatchConflict(deps: BatchConflictBind) {
  const host: BatchConflictHost = {
    openToken: deps.token,
    drawerOpen: () => deps.drawer.value,
    selectedBatchNo: () => deps.selected.value?.batch_no,
    startRead: deps.startRead,
    getBatch: deps.getBatch,
    applyBatch: (batch) => {
      deps.selected.value = batch
    },
    setUnconfirmed: (value) => {
      deps.unconfirmed.value = value
    },
    setPending: (value) => {
      deps.pending.value = value
    },
    closeReschedule: () => {
      deps.rescheduleOpen.value = false
    },
    reloadList: deps.reloadList,
  }
  return {
    retry: async () => {
      const batchNo = deps.selected.value?.batch_no
      if (!batchNo || !deps.drawer.value) return
      await refreshBatchSelection(host, batchNo, deps.token())
    },
    recoverIfConflict: async (error: unknown, batchNo: string) => {
      if (!isStateConflict(error)) return false
      await recoverBatchStateConflict(host, batchNo)
      return true
    },
  }
}

/** 冲突后立刻关掉改期框并暂停操作，再按服务端状态恢复或保持不可确认。 */
export async function recoverBatchStateConflict(host: BatchConflictHost, batchNo: string): Promise<void> {
  const tokenAtStart = host.openToken()
  host.closeReschedule()
  host.setPending(true)
  ElMessage.warning("批次状态已变化，已重新读取最新状态，未再次提交。")
  try {
    await refreshBatchSelection(host, batchNo, tokenAtStart)
  } finally {
    if (host.openToken() === tokenAtStart) host.setPending(false)
  }
}
