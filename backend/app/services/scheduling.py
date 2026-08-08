"""定时批次到点、取消与改期用例。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.services.batch_query import BatchAccessScope


class StateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduledBatch:
    batch_no: str
    category: str
    app_id: int = 0
    dept: str = ""
    quota_date: str = ""
    quota_cost: int = 0
    outbox_persisted: bool = False


class SchedulingRepository(Protocol):
    async def claim_due(self) -> list[ScheduledBatch]: ...

    async def cancel(
        self, batch_no: str, scope: BatchAccessScope
    ) -> ScheduledBatch | None: ...

    async def reschedule(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        scheduled_at: datetime,
        *,
        approval_expire_hours: int,
    ) -> bool: ...


class SchedulingQuota(Protocol):
    async def refund_once(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        event_id: str,
        marker_ttl_s: int,
    ) -> Any: ...


class SchedulingPublisher(Protocol):
    async def enqueue(self, batch_no: str, queue: str) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SchedulingService:
    def __init__(
        self,
        repository: SchedulingRepository,
        quota: SchedulingQuota,
        publisher: SchedulingPublisher,
        *,
        clock: Callable[[], datetime] = utc_now,
        approval_expire_hours: int = 24,
        max_schedule_ahead_days: int = 90,
    ) -> None:
        self.repository = repository
        self.quota = quota
        self.publisher = publisher
        self.clock = clock
        self.approval_expire_hours = approval_expire_hours
        self.max_schedule_ahead_days = max_schedule_ahead_days

    async def dispatch_due(self) -> int:
        batches = await self.repository.claim_due()
        for batch in batches:
            if batch.outbox_persisted:
                continue
            await self.publisher.enqueue(
                batch.batch_no,
                "bulk" if batch.category == "market" else "realtime",
            )
        return len(batches)

    async def cancel(self, batch_no: str, scope: BatchAccessScope) -> None:
        batch = await self.repository.cancel(batch_no, scope)
        if batch is None:
            raise StateConflict("仅 scheduled 批次可取消")
        if not batch.outbox_persisted:
            await self.quota.refund_once(
                app_id=batch.app_id,
                dept=batch.dept,
                category=batch.category,
                date_key=batch.quota_date,
                cost=batch.quota_cost,
                event_id=f"batch:{batch.batch_no}:cancelled",
                marker_ttl_s=172800,
            )

    async def reschedule(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        scheduled_at: datetime,
    ) -> None:
        if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include timezone")
        if scheduled_at <= self.clock():
            raise ValueError("scheduled_at must be in the future")
        if scheduled_at > self.clock() + timedelta(days=self.max_schedule_ahead_days):
            raise ValueError(
                f"scheduled_at 不能超过 {self.max_schedule_ahead_days} 天"
            )
        if not await self.repository.reschedule(
            batch_no,
            scope,
            scheduled_at,
            approval_expire_hours=self.approval_expire_hours,
        ):
            raise StateConflict("仅 scheduled 批次可改期")
