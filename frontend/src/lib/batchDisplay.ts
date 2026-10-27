import type { BatchItem } from "../api/queries"

/** 批次结果构成：各消息状态计数。 */
export interface BatchCompose {
  pending: number
  sent: number
  delivered: number
  failed: number
  unknown: number
  other: number
  cancelled: number
  rejected: number
  expired: number
  needs_review: number
}

// 渠道映射只保留这一处事实源；下拉选项由它派生。
export const channelLabel: Record<string, string> = { api: "API", web: "Web" }

/** 批次号过长时缩写为首 2 位 + 末 4 位。 */
export function shortBatchNo(value: string): string {
  return value.length > 12 ? `${value.slice(0, 2)}…${value.slice(-4)}` : value
}

/** 结果构成直接使用服务端消息状态计数，不从总数反推。 */
export function buildCompose(row: BatchItem): BatchCompose {
  return {
    pending: row.pending,
    sent: row.sent,
    delivered: row.delivered,
    failed: row.failed,
    unknown: row.unknown,
    other: row.other,
    cancelled: row.cancelled,
    rejected: row.rejected,
    expired: row.expired,
    needs_review: row.needs_review,
  }
}

/** 从未下发且批次已终止的条数。不计入待处理。 */
export function terminalUnsent(parts: BatchCompose): number {
  return parts.cancelled + parts.rejected + parts.expired + parts.needs_review
}

/** 未终态条数（待处理 + 已提交）。 */
export function activeOf(row: BatchItem): number {
  return row.pending + row.sent
}

/** 构成份额宽度：占受理总数的百分比，不是成功率。 */
export function composePct(part: number, row: BatchItem): string {
  return row.total > 0 ? `${(part / row.total) * 100}%` : "0%"
}

/** 构成条的读屏文案。 */
export function describeCompose(row: BatchItem, parts: BatchCompose): string {
  return `待处理 ${parts.pending}，待回执 ${parts.sent}，送达 ${parts.delivered}，失败 ${parts.failed}，未知 ${parts.unknown}，其他 ${parts.other}，已取消未下发 ${parts.cancelled}，已驳回未下发 ${parts.rejected}，已过期未下发 ${parts.expired}，待核对 ${parts.needs_review}，占受理总数 ${row.total}`
}
