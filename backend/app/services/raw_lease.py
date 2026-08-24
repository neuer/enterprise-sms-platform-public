"""raw_vendor_log 处理租约：claim token 与终态 fencing 共用合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

RAW_LEASE_SECONDS = 15 * 60


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
  CAST(:processing_lease_id AS uuid),1,now()+interval '15 minutes'
"""

FENCED_TERMINAL_SQL = """
UPDATE raw_vendor_log
SET processed=:processed,error=:error,processing_started_at=NULL,
  parse_state=:parse_state,
  replay_eligibility=:replay_eligibility,
  processing_lease_id=NULL,
  processing_lease_expires_at=NULL
WHERE id=:id
  AND processing_lease_id=CAST(:lease_id AS uuid)
  AND processing_lease_epoch=:epoch
  AND processing_lease_expires_at>now()
"""

FENCED_METADATA_SQL = """
UPDATE raw_vendor_log
SET custom_ids=CAST(:custom_ids AS text[]),item_count=:item_count
WHERE id=:id
  AND processing_lease_id=CAST(:lease_id AS uuid)
  AND processing_lease_epoch=:epoch
  AND processing_lease_expires_at>now()
"""

CLAIM_LEASE_SET_SQL = """
  processing_started_at=now(),error=NULL,
  processing_lease_id=CAST(:lease_id AS uuid),
  processing_lease_epoch=processing_lease_epoch+1,
  processing_lease_expires_at=now()+interval '15 minutes'
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
