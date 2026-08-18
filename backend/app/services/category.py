"""消息类别策略矩阵的唯一实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Category = Literal["verify", "notice", "market"]
QueueName = Literal["realtime", "bulk"]
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_MARKET_WINDOW = "08:00-21:00"


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


def queue_for_category(category: str) -> QueueName:
    """营销走 bulk，验证码/通知走 realtime。"""

    return "bulk" if category == "market" else "realtime"


def coerce_market_dispatch(
    now: datetime,
    window: str,
    scheduled_at: datetime | None,
) -> tuple[Literal["queued", "scheduled"], str | None, datetime | None]:
    """营销不得窗外发送；显式定时若落在窗外也顺延到下一窗开始。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include timezone")
    if scheduled_at is not None and (
        scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None
    ):
        raise ValueError("scheduled_at must include timezone")
    start_raw, end_raw = window.split("-", maxsplit=1)
    start = time.fromisoformat(start_raw)
    end = time.fromisoformat(end_raw)
    if start >= end:
        raise ValueError("market window must be a same-day half-open interval")
    reference = scheduled_at if scheduled_at is not None else now
    local = reference.astimezone(SHANGHAI)
    local_clock = local.time().replace(tzinfo=None)
    if start <= local_clock < end:
        if scheduled_at is not None:
            return "scheduled", None, scheduled_at
        return "queued", None, None
    target_date = (
        local.date() if local_clock < start else local.date() + timedelta(days=1)
    )
    target = datetime.combine(target_date, start, tzinfo=SHANGHAI)
    return "scheduled", "market_window", target
