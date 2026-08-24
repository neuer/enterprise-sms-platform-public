"""raw_vendor_log 处理租约：claim token、续租与终态 fencing 共用合同。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

RAW_LEASE_SECONDS = 15 * 60
RAW_LEASE_HEARTBEAT_SECONDS = RAW_LEASE_SECONDS / 3
SYSTEM_REPLAY_AUDIT_PENDING = "pending"
SYSTEM_REPLAY_AUDIT_COMPLETED = "completed"


class RawLeaseLost(RuntimeError):
    """当前调用方已不再持有处理权；不得覆盖现有状态。"""


@dataclass(frozen=True, slots=True)
class RawProcessingLease:
    raw_id: int
    lease_id: UUID
    epoch: int
    expires_at: datetime | None = None


def new_lease_id() -> UUID:
    return uuid4()


def lease_from_row(row: Any, *, raw_id: int | None = None) -> RawProcessingLease | None:
    """从认领/插入回行构造租约；缺 token 则视为未持有。"""

    lease_id = row.get("processing_lease_id") if hasattr(row, "get") else None
    if lease_id is None:
        return None
    epoch = row.get("processing_lease_epoch") if hasattr(row, "get") else None
    return RawProcessingLease(
        raw_id=int(raw_id if raw_id is not None else row["id"]),
        lease_id=UUID(str(lease_id)),
        epoch=int(epoch or 1),
        expires_at=row.get("processing_lease_expires_at") if hasattr(row, "get") else None,
    )


def remember_if_supported(repository: Any, lease: RawProcessingLease | None) -> None:
    if lease is None:
        return
    remember = getattr(repository, "remember_lease", None)
    if callable(remember):
        remember(lease)


def require_lease(lease: RawProcessingLease | None, raw_id: int) -> RawProcessingLease:
    if lease is None or lease.raw_id != raw_id:
        raise RawLeaseLost("raw processing lease missing")
    return lease


PERSIST_LEASE_COLUMNS = """
  processing_lease_id,processing_lease_epoch,processing_lease_expires_at
"""

PERSIST_LEASE_VALUES = """
  CASE WHEN :acquire_processing_lease THEN CAST(:processing_lease_id AS uuid) END,
  CASE WHEN :acquire_processing_lease THEN 1 ELSE 0 END,
  CASE WHEN :acquire_processing_lease
    THEN now()+make_interval(secs => :lease_seconds) END
"""

PERSIST_STARTED_AT_SQL = """
  CASE WHEN :acquire_processing_lease THEN now() END
"""

FENCED_TERMINAL_SQL = """
UPDATE raw_vendor_log
SET processed=:processed,error=:error,processing_started_at=NULL,
  parse_state=:parse_state,
  replay_eligibility=:replay_eligibility,
  processing_lease_id=NULL,
  processing_lease_expires_at=NULL,
  system_replay_audit_state=COALESCE(
    CAST(:system_replay_audit_state AS varchar),
    system_replay_audit_state
  )
WHERE id=:id
  AND processing_lease_id=CAST(:lease_id AS uuid)
  AND processing_lease_epoch=:epoch
"""

FENCED_METADATA_SQL = """
UPDATE raw_vendor_log
SET custom_ids=CAST(:custom_ids AS text[]),item_count=:item_count
WHERE id=:id
  AND processing_lease_id=CAST(:lease_id AS uuid)
  AND processing_lease_epoch=:epoch
"""

CLAIM_LEASE_SET_SQL = """
  processing_started_at=now(),error=NULL,
  processing_lease_id=CAST(:lease_id AS uuid),
  processing_lease_epoch=processing_lease_epoch+1,
  processing_lease_expires_at=now()+make_interval(secs => :lease_seconds)
"""

HEARTBEAT_LEASE_SQL = """
UPDATE raw_vendor_log
SET processing_lease_expires_at=now()+make_interval(secs => :lease_seconds)
WHERE id=:id
  AND processing_lease_id=CAST(:lease_id AS uuid)
  AND processing_lease_epoch=:epoch
"""

CLAIM_LEASE_PREDICATE_SQL = """
  (
    processing_lease_id IS NULL
    OR processing_lease_expires_at IS NULL
    OR processing_lease_expires_at<=now()
  )
"""

STALE_LEASE_PREDICATE_SQL = """
  (
    processing_lease_id IS NULL
    OR processing_lease_expires_at IS NULL
    OR processing_lease_expires_at<=now()
  )
"""


async def record_raw_fencing_miss(
    connection: Any,
    *,
    raw_id: int,
    lease_id: UUID,
) -> None:
    """记录无 PII 的 raw fencing_miss；失败向外抛出。"""

    await connection.execute(
        text(
            """
            INSERT INTO worker_lease_event(task_kind,task_id,event_type,lease_id)
            VALUES ('raw',:task_id,'fencing_miss',CAST(:lease_id AS uuid))
            """
        ),
        {"task_id": raw_id, "lease_id": str(lease_id)},
    )


async def execute_fenced_raw_update(
    connection: Any,
    sql: str,
    params: dict[str, Any],
    *,
    lease: RawProcessingLease,
) -> bool:
    """执行带 lease CAS 的 UPDATE。

    未命中时在同一事务写入 fencing_miss，返回 False；调用方必须先提交
    再抛 RawLeaseLost，避免事件与空 UPDATE 一起回滚。
    """

    result = await connection.execute(text(sql), params)
    rowcount = getattr(result, "rowcount", 1)
    if rowcount == 0:
        await record_raw_fencing_miss(
            connection, raw_id=lease.raw_id, lease_id=lease.lease_id
        )
        return False
    return True


async def commit_fenced_raw_update(
    engine: Any,
    sql: str,
    params: dict[str, Any],
    *,
    lease: RawProcessingLease,
) -> None:
    """提交 fencing 事务后再失败关闭，保证 miss 事件可观测。"""

    async with engine.begin() as connection:
        applied = await execute_fenced_raw_update(
            connection, sql, params, lease=lease
        )
    if not applied:
        raise RawLeaseLost("raw processing lease lost")


async def record_raw_heartbeat_lost(
    connection: Any,
    *,
    raw_id: int,
    lease_id: UUID,
) -> None:
    """记录无 PII 的 raw heartbeat_lost；失败向外抛出。"""

    await connection.execute(
        text(
            """
            INSERT INTO worker_lease_event(task_kind,task_id,event_type,lease_id)
            VALUES ('raw',:task_id,'heartbeat_lost',CAST(:lease_id AS uuid))
            """
        ),
        {"task_id": raw_id, "lease_id": str(lease_id)},
    )


async def renew_raw_lease(
    engine: Any,
    lease: RawProcessingLease,
    *,
    lease_seconds: int = RAW_LEASE_SECONDS,
) -> None:
    """仅当前 lease_id+epoch 可续租；miss 先提交 heartbeat_lost 再失败关闭。"""

    async with engine.begin() as connection:
        result = await connection.execute(
            text(HEARTBEAT_LEASE_SQL),
            {
                "id": lease.raw_id,
                "lease_id": str(lease.lease_id),
                "epoch": lease.epoch,
                "lease_seconds": lease_seconds,
            },
        )
        applied = getattr(result, "rowcount", 1) != 0
        if not applied:
            await record_raw_heartbeat_lost(
                connection, raw_id=lease.raw_id, lease_id=lease.lease_id
            )
    if not applied:
        raise RawLeaseLost("raw processing lease heartbeat lost")


class RawLeaseHeartbeat:
    """长数组处理期间按合同续租；失败后旧处理器必须停止后续业务写入。"""

    def __init__(
        self,
        renew: Callable[[RawProcessingLease], Awaitable[None]],
        lease: RawProcessingLease,
        *,
        interval_s: float = RAW_LEASE_HEARTBEAT_SECONDS,
    ) -> None:
        self._renew = renew
        self.lease = lease
        self.interval_s = interval_s
        self._lost: RawLeaseLost | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def raise_if_lost(self) -> None:
        if self._lost is not None:
            raise self._lost

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                pass
            try:
                await self._renew(self.lease)
            except RawLeaseLost as error:
                self._lost = error
                return

    async def __aenter__(self) -> RawLeaseHeartbeat:
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, RawLeaseLost):
                await self._task


@asynccontextmanager
async def bind_raw_lease_heartbeat(
    repository: Any,
    lease: RawProcessingLease | None,
    *,
    interval_s: float = RAW_LEASE_HEARTBEAT_SECONDS,
) -> AsyncIterator[RawLeaseHeartbeat | None]:
    """仓储未实现续租或尚无 token 时跳过，避免假对象被误打到数据库。"""

    renewer = getattr(repository, "renew_processing_lease", None)
    if lease is None or not callable(renewer):
        yield None
        return
    async with RawLeaseHeartbeat(renewer, lease, interval_s=interval_s) as beat:
        yield beat
