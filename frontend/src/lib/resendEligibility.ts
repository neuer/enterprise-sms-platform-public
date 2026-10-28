/** 与后端重发资格同一套原因。只展示枚举，不拼接号码。 */

const RESEND_BLOCK_TEXT: Record<string, string> = {
  receipts_pending: "仍有回执未到齐，暂不能重发",
  source_terminated: "批次已终止，未下发号码不能重发",
  not_terminal: "批次尚未完成，暂不能重发",
  supplement_required: "已有重发未覆盖全部失败号码。请补发未覆盖号码，不要更换业务号",
  coverage_needs_review: "已有重发无法唯一对应原失败号码，需要人工核对",
  nothing_uncovered: "没有未覆盖的失败号码",
  preview_stale: "补发预览中的号码已不再是失败，请重新预览",
}

export function resendBlockText(reason: string | null | undefined): string | null {
  if (!reason) return null
  return RESEND_BLOCK_TEXT[reason] ?? "当前不能重发"
}
