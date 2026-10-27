"""消息展示状态。只读投影，不改短信或批次的持久状态。"""

from __future__ import annotations

# 真实投递结果保持原状态。pending 且批次已取消/驳回/过期时，表示从未下发的终止。
MESSAGE_DISPLAY_STATUS_SQL = """
CASE
  WHEN m.status IN ('sent','delivered','failed','unknown','other') THEN m.status
  WHEN m.status='pending' AND b.status IN ('cancelled','rejected','expired')
    THEN b.status
  WHEN m.status='pending' AND b.status IN (
    'pending_approval','scheduled','queued','sending','balance_blocked'
  ) THEN 'pending'
  ELSE 'needs_review'
END
""".strip()

DISPLAY_STATUS_VALUES = frozenset(
    {
        "pending",
        "sent",
        "delivered",
        "failed",
        "unknown",
        "other",
        "cancelled",
        "rejected",
        "expired",
        "needs_review",
    }
)


def ensure_display_status(value: str | None) -> None:
    """拒绝未登记的展示状态，避免筛选条件与投影分叉。"""

    if value is not None and value not in DISPLAY_STATUS_VALUES:
        raise ValueError("invalid display status")


def display_status_predicate(param: str = "display_status") -> str:
    """与 SELECT 使用同一表达式，count 和 rows 不能各写一套。"""

    return (
        f"(CAST(:{param} AS varchar(16)) IS NULL OR "
        f"({MESSAGE_DISPLAY_STATUS_SQL})=:{param})"
    )


def display_count(name: str) -> str:
    """按展示状态计数。name 必须来自本模块的固定集合。"""

    if name not in DISPLAY_STATUS_VALUES:
        raise ValueError("invalid display status")
    return (
        "CAST(count(*) FILTER (WHERE "
        f"({MESSAGE_DISPLAY_STATUS_SQL})='{name}') AS integer) AS {name}"
    )
