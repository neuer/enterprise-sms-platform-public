"""用量账本的共享类型、校验模式与上海自然日时钟。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


HMAC_PATTERN = re.compile(r"^[0-9a-f]{64}$")


DATE_KEY_PATTERN = re.compile(r"^[0-9]{8}$")


UUID_FRAGMENT = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


REQUEST_KEY_PATTERN = re.compile(
    rf"^(?:"
    rf"acceptance:(?:v2:[0-9a-f]{{64}}:[0-9]{{8}}|"
    rf"[0-9]+:[0-9a-f]{{64}}:[0-9]{{8}}|{UUID_FRAGMENT})"
    rf"|legacy:batch:[0-9a-f]{{32}}"
    rf")$"
)


RELEASE_EVENT_ID_PATTERN = re.compile(
    rf"^(?:"
    rf"batch:[0-9a-f]{{32}}:cancelled"
    rf"|approval:[1-9][0-9]*:(?:rejected|expired)"
    rf"|usage:{UUID_FRAGMENT}:"
    rf"(?:acceptance-failed|all-filtered|idempotent-reuse|orphan-recovery|"
    rf"uncertain-retry|uncertain-unused)"
    rf")$"
)


class UsageRedis(Protocol):
    async def eval(self, *args: Any) -> Any: ...

    async def get(self, name: str) -> Any: ...

    async def set(self, name: str, value: Any, **kwargs: Any) -> Any: ...

    async def mget(self, keys: Sequence[str]) -> list[Any]: ...


class UsageProjectionUnavailable(RuntimeError):
    """Redis 投影缺失或不可确认，受理必须失败关闭。"""


class UsageReservationConflict(RuntimeError):
    """同一稳定请求引用出现不一致的账本合同。"""


@dataclass(frozen=True, slots=True)
class UsageReservation:
    reservation_id: UUID
    reused: bool = False


@dataclass(frozen=True, slots=True)
class FrequencyDecisionItem:
    """单次频控决策输入；大批量受理按批合并进同一事务。"""

    phone_hmac: str
    hmac_aliases: Mapping[int, str]


@dataclass(frozen=True, slots=True)
class ResolvedFrequencySubject:
    """一次受理内只解析一次的频控主体；决策阶段不得再查库定位。"""

    phone_hmac: str
    hmac_aliases: Mapping[int, str]
    subject_id: UUID
    projection_hmac: str


FREQUENCY_DECISION_CHUNK = 200


@dataclass(frozen=True, slots=True)
class UsageDrift:
    quota_mismatches: int
    quota_delta: int
    frequency_mismatches: int
    frequency_delta: int

    @property
    def mismatches(self) -> int:
        return self.quota_mismatches + self.frequency_mismatches


def utc_now() -> datetime:
    return datetime.now(UTC)


def shanghai_day(now: datetime) -> tuple[str, date, datetime]:
    """返回上海自然日键、日期和下一日边界。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("usage ledger clock must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    next_day = (local + timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return local.strftime("%Y%m%d"), local.date(), next_day


def frequency_windows(now: datetime) -> tuple[str, datetime, str, date, datetime]:
    """返回 UTC 分钟窗与上海自然日窗。"""

    date_key, usage_date, next_day = shanghai_day(now)
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    minute_window = str(int(now.timestamp() // 60))
    return minute_window, next_minute, date_key, usage_date, next_day


def _safe_request_key(value: str) -> str:
    if REQUEST_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid usage reservation request key")
    return value


def _safe_event_id(value: str) -> str:
    if RELEASE_EVENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid usage release event id")
    return value
