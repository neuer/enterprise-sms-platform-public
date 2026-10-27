import type { ApprovalListItem, DecisionOutcome } from "../api/approvals"
import type { Category } from "../api/webMessages"
import { formatDateTime } from "./time"

/** 打开确认时冻结的审阅版本。轮询到的新对象不能替换它。 */
export interface ApprovalReview {
  id: number
  generation: number
  scheduledAt: string | null
}

export function captureReview(item: Pick<ApprovalListItem, "id" | "generation" | "scheduled_at">): ApprovalReview {
  return { id: item.id, generation: item.generation, scheduledAt: item.scheduled_at }
}

export function reviewDrifted(review: ApprovalReview, items: readonly ApprovalListItem[]): boolean {
  const latest = items.find((item) => item.id === review.id)
  return latest !== undefined && latest.generation !== review.generation
}

export const REVIEW_STALE_TEXT = "申请已改期，请重新打开并确认当前发送计划"

export function formatSchedule(value: string | null): string {
  return value ? formatDateTime(value) : "立即发送"
}

export function deciderLabel(item: Pick<ApprovalListItem, "approver" | "status">): string | null {
  if (item.approver) return item.approver
  if (item.status === "expired") return "系统自动"
  return null
}

export function drawerStatusLine(status: string, countdown: string | null, label: string): string {
  if (status === "pending" && countdown && countdown !== "已临期截止") return `${label} · 剩 ${countdown}`
  if (status === "pending" && countdown === "已临期截止") return `${label} · 已临期截止`
  return label
}

export function laneLabel(category: Category): string {
  return category === "market" ? "批量通道" : "实时通道"
}

export function previewDecision(item: ApprovalListItem, schedule: string): string {
  if (item.scheduled_at) return `通过后 → 已排期 · ${schedule}`
  return `通过后 → 排队中 · ${laneLabel(item.category)}`
}

export function outcomeText(outcome: DecisionOutcome, lane: string): string {
  if (outcome.batch_status === "scheduled") {
    return outcome.deferred_reason === "market_window"
      ? "当前处于营销时间窗外，批次已改派为定时发送"
      : "批次已进入定时发送计划"
  }
  if (outcome.batch_status === "queued") return `批次已进入发送队列（${lane}）`
  return "批次已受理进入发送流程"
}
