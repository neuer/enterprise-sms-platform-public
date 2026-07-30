"""消息类别策略矩阵的唯一实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["verify", "notice", "market"]
QueueName = Literal["realtime", "bulk"]


class CategoryNotAllowed(PermissionError):
    """应用未获准发送该类别，对应 CATEGORY_NOT_ALLOWED/403。"""


@dataclass(frozen=True, slots=True)
class CategoryPolicy:
    category: Category
    queue: QueueName
    blacklist_required: bool
    approval_threshold_key: str | None
    qps_lane: Literal["realtime", "bulk"]


def policy_for_category(
    category: str,
    allowed_categories: set[str] | frozenset[str],
    *,
    notice_blacklist: bool = True,
) -> CategoryPolicy:
    """校验类别授权并返回集中式队列/黑名单/审批/QPS 策略。"""

    if category not in {"verify", "notice", "market"}:
        raise ValueError("category must be verify, notice or market")
    if category not in allowed_categories:
        raise CategoryNotAllowed("应用无权发送该消息类别")
    if category == "verify":
        return CategoryPolicy("verify", "realtime", False, None, "realtime")
    if category == "notice":
        return CategoryPolicy(
            "notice",
            "realtime",
            notice_blacklist,
            "approval_threshold",
            "realtime",
        )
    return CategoryPolicy(
        "market",
        "bulk",
        True,
        "market_approval_threshold",
        "bulk",
    )
