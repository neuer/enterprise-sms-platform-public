"""运维中心安全查询的领域模型与输入归一化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Literal, Protocol, TypeVar

from app.core.auth.accounts import SecurityPrincipal
from app.core.jobtrack import JobSpec
from app.services.category import queue_for_category
from app.services.outbox import MANUAL_JOB_TASK_NAMES
from app.services.queue_pause import parse_queue_pause_claim

T = TypeVar("T")
AlertLevel = Literal["info", "warn", "crit"]
RawSource = Literal["report", "reply"]


@dataclass(frozen=True, slots=True)
class OpsPage(Generic[T]):  # noqa: UP046 - 兼容宿主 Python 3.9 静态检查器
    items: tuple[T, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: int
    alert_type: str
    level: AlertLevel
    title: str
    detail: dict[str, Any] | None
    channels: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RawLogRecord:
    id: int
    source: RawSource
    item_count: int
    custom_id_count: int
    processed: bool
    error: str | None
    fetched_at: datetime
    capture_state: str = "complete"
    parse_state: str = "unattempted"
    replay_eligibility: str = "manual"


@dataclass(frozen=True, slots=True)
class UncertainRecord:
    chunk_id: int
    batch_no: str
    custom_id: str
    phone_count: int
    vendor_code: int | None
    uncertain_since: datetime
    age_seconds: int
    status: str = "uncertain"
    resolution_id: int | None = None
    resolution_action: str | None = None
    resolution_state: str | None = None
    proposer_account_id: int | None = None


@dataclass(frozen=True, slots=True)
class UnmatchedRecord:
    id: int
    vendor_task_id: str | None
    custom_id: str | None
    phone_mask: str
    report_status: int | None
    report_desc: str | None
    report_time: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AlertQuery:
    alert_type: str | None
    level: AlertLevel | None
    start: datetime | None
    end: datetime | None
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class RawLogQuery:
    source: RawSource | None
    processed: bool | None
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class UnmatchedQuery:
    phone_hmacs: tuple[str, ...]
    start: datetime | None
    end: datetime | None
    page: int
    page_size: int


class OpsRepository(Protocol):
    async def list_unmatched(
        self,
        query: UnmatchedQuery,
    ) -> OpsPage[UnmatchedRecord]: ...


class HmacProvider(Protocol):
    def hmac_candidates(self, phone: str) -> dict[int, str]: ...


class JobNotFound(LookupError):
    """任务不在 tracked beat 固定 allowlist。"""


@dataclass(frozen=True, slots=True)
class JobRoute:
    task_name: str
    queue: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_name: str
    last_run_at: datetime | None
    last_status: str | None
    last_duration_ms: int | None
    last_items: int
    success_rate_24h: float
    stalled: bool


class JobOpsRepository(Protocol):
    async def list_jobs(
        self,
        specs: Sequence[JobSpec],
        *,
        now: datetime,
    ) -> tuple[JobRecord, ...]: ...

    async def audit_job_trigger(
        self,
        job_name: str,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> None: ...


class JobSender(Protocol):
    async def send(self, task_name: str, queue: str) -> None: ...


class JobOpsService:
    """只允许声明过且有固定无参路由的任务手动触发。"""

    def __init__(
        self,
        repository: JobOpsRepository,
        sender: JobSender,
        specs: Mapping[str, JobSpec],
        routes: Mapping[str, JobRoute],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.sender = sender
        self.specs = dict(specs)
        self.routes = dict(routes)
        self.clock = clock

    async def list(self) -> tuple[JobRecord, ...]:
        selected = tuple(self.specs[name] for name in sorted(self.specs))
        return await self.repository.list_jobs(selected, now=self.clock())

    async def trigger(
        self,
        job_name: str,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> None:
        route = self.routes.get(job_name)
        if (
            job_name not in self.specs
            or route is None
            or route.task_name not in MANUAL_JOB_TASK_NAMES
        ):
            # 允许清单校验先于审计：不可触发的任务 404，且不留下
            # 一条从未发生的触发审计。
            raise JobNotFound(job_name)
        await self.repository.audit_job_trigger(
            job_name,
            actor=actor,
            ip=ip,
            principal=principal,
        )
        await self.sender.send(route.task_name, route.queue)


class QueueResumeConflict(RuntimeError):
    """暂停原因或余额状态不允许普通恢复。"""


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    realtime_code: str | None
    bulk_code: str | None
    balance: int | None
    threshold: int


@dataclass(frozen=True, slots=True)
class PausedBatch:
    batch_no: str
    category: str


@dataclass(frozen=True, slots=True)
class QueueResumeResult:
    resumed_batches: int
    paused_codes: tuple[str, ...]


class QueueRecoveryRepository(Protocol):
    async def queue_snapshot(self) -> QueueSnapshot: ...

    async def resume_batches(
        self,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> tuple[PausedBatch, ...]: ...

    async def clear_queue_pauses(self) -> None: ...


class QueueBatchSender(Protocol):
    async def send_batch(self, batch_no: str, lane: str) -> None: ...


class QueueRecoveryService:
    """显式检查暂停原因后恢复 DB 事实、Redis 通道与批次投递。"""

    def __init__(
        self,
        repository: QueueRecoveryRepository,
        sender: QueueBatchSender,
    ) -> None:
        self.repository = repository
        self.sender = sender

    async def status(self) -> QueueSnapshot:
        return await self.repository.queue_snapshot()

    async def resume(
        self,
        *,
        force: bool,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> QueueResumeResult:
        snapshot = await self.repository.queue_snapshot()
        codes = tuple(
            sorted(
                {
                    parsed
                    for parsed in (
                        parse_queue_pause_claim(snapshot.realtime_code),
                        parse_queue_pause_claim(snapshot.bulk_code),
                    )
                    if parsed is not None
                }
            )
        )
        if not force:
            if snapshot.balance is None or snapshot.balance < snapshot.threshold:
                raise QueueResumeConflict("厂商余额尚未达到恢复阈值")
            if any(code != "999" for code in codes):
                raise QueueResumeConflict("非余额熔断必须显式设置 force=true")
        batches = await self.repository.resume_batches(
            actor=actor,
            ip=ip,
            principal=principal,
        )
        await self.repository.clear_queue_pauses()
        for batch in batches:
            lane = queue_for_category(batch.category)
            await self.sender.send_batch(batch.batch_no, lane)
        return QueueResumeResult(len(batches), codes)


def validate_range(start: datetime | None, end: datetime | None) -> None:
    for moment in (start, end):
        if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
            raise ValueError("ops time must include timezone")
    if start is not None and end is not None and start > end:
        raise ValueError("ops start must not be later than end")


class OpsService:
    """在仓储前完成时间校验和手机号不可逆 HMAC 转换。"""

    def __init__(self, repository: OpsRepository, crypto: HmacProvider) -> None:
        self.repository = repository
        self.crypto = crypto

    async def list_unmatched(
        self,
        phone: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        page_size: int,
    ) -> OpsPage[UnmatchedRecord]:
        validate_range(start, end)
        phone_hmacs = (
            tuple(self.crypto.hmac_candidates(phone).values()) if phone is not None else ()
        )
        return await self.repository.list_unmatched(
            UnmatchedQuery(phone_hmacs, start, end, page, page_size)
        )
