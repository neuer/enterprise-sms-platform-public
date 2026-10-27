/** 与后端手动重推资格同一套原因文案。只展示枚举，不拼接 URL 或密钥。 */

const RETRY_BLOCK_TEXT: Record<string, string> = {
  config_revoked: "旧回调已因配置变更撤销，恢复配置也不能重推",
  app_disabled: "应用已停用，不能重推",
  config_changed: "回调地址或密钥已变化，不能重推这条旧任务",
  detail_disabled: "明细回调已关闭，不能重推这条旧任务",
  not_dead: "仅 dead 回调可手动重推",
}

export function callbackRetryReason(task: {
  manual_retry_allowed: boolean
  retry_block_reason?: string | null
}): string | null {
  if (task.manual_retry_allowed) return null
  if (!task.retry_block_reason) return "当前不能重推"
  return RETRY_BLOCK_TEXT[task.retry_block_reason] ?? "当前不能重推"
}
