/**
 * 发送准入 503（DEPENDENCY_UNAVAILABLE）detail.reason 的中文解释单点。
 * reason 由 services/send_admission.py 的 evaluate_capacity 产出；未知值原样回显，不吞诊断信息。
 */
export const ADMISSION_REASON_TEXT: Readonly<Record<string, string>> = {
  queues_paused: "实时与批量队列均被暂停",
  queue_paused: "部分发送队列被暂停",
  realtime_paused: "实时发送队列已暂停",
  bulk_paused: "批量发送队列已暂停",
  outbox_backlog: "投递通道积压超限",
  outbox_oldest: "投递通道最老事件滞留超时",
  outbox_dead: "投递通道死信过多",
  callback_backlog: "回调投递积压",
  uncertain_overdue: "结果未知分片超时未处置",
  vendor_failures: "厂商接口连续失败",
  snapshot_unavailable: "准入快照暂不可用（失败关闭）",
  dispatcher_heartbeat_stale: "投递调度心跳过期",
  send_lanes_heartbeat_stale: "发送通道心跳过期",
  realtime_heartbeat_stale: "实时发送通道心跳过期",
  bulk_heartbeat_stale: "批量发送通道心跳过期",
  degraded_bulk: "平台降级保护中，营销批量发送暂不放行",
  degraded_volume: "平台降级保护中，仅放行小批量发送，请减少号码量后重试",
  recovery_volume: "平台恢复保护中，仅放行小批量发送，请减少号码量或稍后重试",
  recovery_segment_cost: "平台恢复保护中，仅放行小内容量发送，请稍后重试",
}

/** 从错误 detail 提取准入 reason；非准入错误返回 null。 */
export function admissionReasonOf(error: unknown): string | null {
  if (typeof error !== "object" || error === null) return null
  const detail = (error as { detail?: unknown }).detail
  if (typeof detail !== "object" || detail === null) return null
  const reason = (detail as { reason?: unknown }).reason
  return typeof reason === "string" && reason ? reason : null
}

/** 准入 reason 的中文解释；未知值返回 null，由调用方回退到通用文案。 */
export function admissionReasonText(reason: string): string | null {
  return ADMISSION_REASON_TEXT[reason] ?? null
}
