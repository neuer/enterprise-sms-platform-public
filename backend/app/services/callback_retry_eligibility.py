"""手动重推资格。与 callback_task 更新条件同一顺序，只产出枚举。"""

from __future__ import annotations


class CallbackRetryConflict(RuntimeError):
    """手动重推被拒绝。reason 是稳定枚举，文案给操作者。"""

    def __init__(self, message: str, reason: str = "not_dead") -> None:
        super().__init__(message)
        self.reason = reason


# 不投影 URL 或密钥。
RETRY_BLOCK_REASON_SQL = """
CASE
  WHEN t.status <> 'dead' THEN 'not_dead'
  WHEN t.last_error = 'CallbackConfigRevoked' THEN 'config_revoked'
  WHEN a.status IS DISTINCT FROM 1 THEN 'app_disabled'
  WHEN a.callback_url IS DISTINCT FROM t.url
    OR a.callback_secret_enc IS DISTINCT FROM t.callback_secret_enc
    THEN 'config_changed'
  WHEN t.event = 'message.report'
    AND a.callback_report_enabled IS DISTINCT FROM true
    THEN 'detail_disabled'
  ELSE NULL
END
"""

_RETRY_BLOCK_MESSAGES = {
    "not_dead": "仅 dead 回调可手动重推",
    "config_revoked": "旧回调已因配置变更撤销，恢复配置也不能重推",
    "app_disabled": "应用已停用，不能重推",
    "config_changed": "回调地址或密钥已变化，不能重推这条旧任务",
    "detail_disabled": "明细回调已关闭，不能重推这条旧任务",
}


def list_retry_columns() -> str:
    """列表摘要里的资格列。"""

    reason = RETRY_BLOCK_REASON_SQL
    return f"({reason}) IS NULL AS manual_retry_allowed, {reason} AS retry_block_reason"


def diagnosis_sql() -> str:
    """失败后读取与更新条件一致的原因，不返回 URL 或密钥。"""

    return (
        "SELECT "
        + RETRY_BLOCK_REASON_SQL
        + " AS retry_block_reason FROM callback_task t"
        + " JOIN app a ON a.id=t.app_id WHERE t.id=:task_id"
    )


def conflict_for_reason(reason: object | None) -> CallbackRetryConflict:
    key = str(reason or "not_dead")
    message = _RETRY_BLOCK_MESSAGES.get(key, "回调状态已变化，请刷新后重试")
    return CallbackRetryConflict(message, key)
