"""本地回执超时收敛：只依赖 PostgreSQL 消息事实与策略，不接触供应商。"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.report_repository import SqlReportRepository
from app.services.runtime_policy import InvalidRuntimePolicy
from app.settings import Settings, get_settings

DEFAULT_SCAN_SECONDS = 60
MIN_SCAN_SECONDS = 10
MAX_SCAN_SECONDS = 3_600
DEFAULT_BATCH_LIMIT = 50
MAX_BATCH_LIMIT = 500
DEFAULT_MESSAGE_LIMIT = 200
MAX_MESSAGE_LIMIT = 2_000
DEFAULT_ROUND_SECONDS = 20
MAX_ROUND_SECONDS = 120
DEFAULT_STATEMENT_TIMEOUT_MS = 5_000
MAX_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_LOCK_TIMEOUT_MS = 1_000
MAX_LOCK_TIMEOUT_MS = 10_000
MAX_TIMEOUT_HOURS = 720


class ReportTimeoutStorageError(RuntimeError):
    """本轮没有任何成功提交且存储失败，必须单独报告。"""


@dataclass(frozen=True, slots=True)
class SweepResult:
    """一轮有界超时扫描的计数；达到预算不得伪装成全库排空。"""

    candidates: int
    batches_changed: int
    messages_changed: int
    skipped_locked: int
    failed: int
    more_remaining: bool


@dataclass(frozen=True, slots=True)
class SweepBounds:
    """单轮扫描的正数上界。"""

    batch_limit: int
    message_limit_per_batch: int
    max_round_seconds: float
    statement_timeout_ms: int
    lock_timeout_ms: int


def parse_report_timeout_hours(raw: object) -> int:
    """策略缺失或损坏时 fail-closed，不把超时当成 0 或无限大。"""

    if raw is None:
        raise InvalidRuntimePolicy("report_timeout_hours 缺失")
    text = str(raw).strip()
    if not text.isdigit():
        raise InvalidRuntimePolicy("report_timeout_hours 必须为正整数")
    value = int(text)
    if value < 1:
        raise InvalidRuntimePolicy("report_timeout_hours 必须为正整数")
    if value > MAX_TIMEOUT_HOURS:
        raise InvalidRuntimePolicy(f"report_timeout_hours 不得大于 {MAX_TIMEOUT_HOURS}")
    return value


def _require_int(name: str, raw: object, *, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidRuntimePolicy(f"{name} 必须为正整数")
    if raw < minimum or raw > maximum:
        raise InvalidRuntimePolicy(f"{name} 必须在 {minimum}-{maximum} 之间")
    return raw


def _require_positive_seconds(name: str, raw: object, *, maximum: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InvalidRuntimePolicy(f"{name} 必须为正数")
    value = float(raw)
    if value <= 0 or value > maximum:
        raise InvalidRuntimePolicy(f"{name} 必须在 0 与 {maximum} 之间")
    return value


def _optional_int(explicit: int | None, stored: str | None, default: int) -> int:
    if explicit is not None:
        return explicit
    if stored is None or stored == "":
        return default
    if not stored.strip().isdigit():
        raise InvalidRuntimePolicy("超时扫描整数参数损坏")
    return int(stored)


def _optional_float(explicit: float | None, stored: str | None, default: float) -> float:
    if explicit is not None:
        return explicit
    if stored is None or stored == "":
        return default
    try:
        value = float(stored)
    except ValueError as error:
        raise InvalidRuntimePolicy("超时扫描时间预算损坏") from error
    return value


def validate_sweep_bounds(
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    message_limit_per_batch: int = DEFAULT_MESSAGE_LIMIT,
    max_round_seconds: float = DEFAULT_ROUND_SECONDS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
) -> SweepBounds:
    """校验每轮批次、消息与时间预算，拒绝非正数和越界。"""

    return SweepBounds(
        batch_limit=_require_int(
            "batch_limit", batch_limit, minimum=1, maximum=MAX_BATCH_LIMIT
        ),
        message_limit_per_batch=_require_int(
            "message_limit_per_batch",
            message_limit_per_batch,
            minimum=1,
            maximum=MAX_MESSAGE_LIMIT,
        ),
        max_round_seconds=_require_positive_seconds(
            "max_round_seconds", max_round_seconds, maximum=MAX_ROUND_SECONDS
        ),
        statement_timeout_ms=_require_int(
            "statement_timeout_ms",
            statement_timeout_ms,
            minimum=100,
            maximum=MAX_STATEMENT_TIMEOUT_MS,
        ),
        lock_timeout_ms=_require_int(
            "lock_timeout_ms",
            lock_timeout_ms,
            minimum=50,
            maximum=MAX_LOCK_TIMEOUT_MS,
        ),
    )


class ReportTimeoutService:
    """独立于 GetReport 的本地超时服务；禁止创建供应商或 raw spill。"""

    def __init__(
        self,
        repository: SqlReportRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or SqlReportRepository(self.settings)

    async def expire_due_reports(
        self,
        batch_limit: int | None = None,
        message_limit_per_batch: int | None = None,
        max_round_seconds: float | None = None,
        *,
        timeout_hours: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> SweepResult:
        """读取合法超时策略后，按有界短事务收敛到期 sent 消息。"""

        needs_stored = any(
            value is None
            for value in (
                batch_limit,
                message_limit_per_batch,
                max_round_seconds,
                statement_timeout_ms,
                lock_timeout_ms,
            )
        )
        stored = await self.repository.load_sweep_config() if needs_stored else {}
        bounds = validate_sweep_bounds(
            batch_limit=_optional_int(
                batch_limit,
                stored.get("report_timeout_batch_limit"),
                DEFAULT_BATCH_LIMIT,
            ),
            message_limit_per_batch=_optional_int(
                message_limit_per_batch,
                stored.get("report_timeout_message_limit"),
                DEFAULT_MESSAGE_LIMIT,
            ),
            max_round_seconds=_optional_float(
                max_round_seconds,
                stored.get("report_timeout_round_seconds"),
                DEFAULT_ROUND_SECONDS,
            ),
            statement_timeout_ms=_optional_int(
                statement_timeout_ms,
                stored.get("report_timeout_statement_ms"),
                DEFAULT_STATEMENT_TIMEOUT_MS,
            ),
            lock_timeout_ms=_optional_int(
                lock_timeout_ms,
                stored.get("report_timeout_lock_ms"),
                DEFAULT_LOCK_TIMEOUT_MS,
            ),
        )
        hours = (
            parse_report_timeout_hours(timeout_hours)
            if timeout_hours is not None
            else await self.repository.require_report_timeout_hours()
        )
        result = await self.repository.expire_due_reports(
            timeout_hours=hours,
            batch_limit=bounds.batch_limit,
            message_limit_per_batch=bounds.message_limit_per_batch,
            max_round_seconds=bounds.max_round_seconds,
            statement_timeout_ms=bounds.statement_timeout_ms,
            lock_timeout_ms=bounds.lock_timeout_ms,
        )
        if result.failed > 0 and result.batches_changed == 0 and result.messages_changed == 0:
            raise ReportTimeoutStorageError("report timeout storage failed without commits")
        return result
