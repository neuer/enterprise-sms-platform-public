"""batch.finished 的不可变汇总。投递只读本表，不回读批次当前计数。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import current_correlation_id
from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.services.callback import BatchFinishedData
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.settings import Settings, get_settings

_TERMINAL = "'completed','completed_unknown','cancelled','expired','rejected'"


class CallbackSnapshotUnavailable(LookupError):
    """没有可投递的冻结汇总。不得改读 sms_batch 的当前计数。"""


class FinishedRecoveryRejected(Exception):
    """补发新的批次终态被拒绝。missing 为真时任务不存在。"""

    def __init__(self, message: str, reason: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.missing = missing


def finished_snapshot_column() -> str:
    """列表中的汇总状态。不返回计数、正文或回调体。"""

    return """
CASE
  WHEN t.event<>'batch.finished' THEN NULL
  WHEN e.legacy_unavailable THEN 'legacy'
  WHEN e.id IS NOT NULL THEN 'current'
  ELSE 'missing'
END
""".strip()


FINISHED_SNAPSHOT_SQL = """
SELECT trim(batch_no) batch_no,biz_id,category,status,total,
  delivered,failed,unknown_cnt unknown,finished_at,
  legacy_unavailable,manual_resolution_required
FROM callback_batch_event
WHERE task_id=:task_id
"""


def batch_from_snapshot(row: RowMapping | Mapping[str, Any]) -> BatchFinishedData:
    """用冻结行构造回调体。legacy 或残缺行直接拒绝。"""

    if row.get("legacy_unavailable") is not False or row.get("status") is None:
        raise CallbackSnapshotUnavailable("历史批次汇总没有可信快照")
    finished_at = row.get("finished_at")
    flag = row.get("manual_resolution_required")
    if (
        not isinstance(finished_at, datetime)
        or finished_at.tzinfo is None
        or finished_at.utcoffset() is None
        or not isinstance(flag, bool)
    ):
        raise CallbackSnapshotUnavailable("批次汇总冻结字段不可用")
    unknown = int(row["unknown"])
    biz_id = row.get("biz_id")
    return BatchFinishedData(
        batch_no=str(row["batch_no"]).strip(),
        biz_id=None if biz_id is None else str(biz_id),
        category=str(row["category"]),
        status=str(row["status"]),
        total=int(row["total"]),
        delivered=int(row["delivered"]),
        failed=int(row["failed"]),
        unknown=unknown,
        finished_at=finished_at,
        unknown_count=unknown,
        manual_resolution_required=flag,
    )


ENQUEUE_BATCH_FINISHED_SQL = f"""
WITH task_lock AS (
  SELECT pg_advisory_xact_lock(
    hashtextextended('batch.finished:' || CAST(:batch_id AS text),0)
  )
), candidate AS (
  SELECT b.id batch_id,b.app_id,a.callback_url,a.callback_secret_enc,
    trim(b.batch_no) batch_no,b.biz_id,b.category,b.status,
    b.total,b.delivered,b.failed,b.unknown_cnt
  FROM sms_batch b
  JOIN app a ON a.id=b.app_id
  WHERE b.id=CAST(:batch_id AS bigint)
    AND b.status IN ({_TERMINAL})
    AND a.status=1 AND a.callback_url IS NOT NULL
    AND a.callback_secret_enc IS NOT NULL
), inserted_task AS (
  INSERT INTO callback_task(
    correlation_id,app_id,event,batch_id,source_report_event_key,url,
    callback_secret_enc,callback_secret_key_version,signature_version
  )
  SELECT :correlation_id,c.app_id,'batch.finished',c.batch_id,
    CAST(:source_report_event_key AS char(64)),c.callback_url,
    c.callback_secret_enc,
    ((get_byte(c.callback_secret_enc,0) << 8)
      + get_byte(c.callback_secret_enc,1))::smallint,
    1
  FROM candidate c,task_lock
  WHERE CAST(:source_report_event_key AS char(64)) IS NOT NULL
     OR NOT EXISTS (
       SELECT 1 FROM callback_task t
       WHERE t.event='batch.finished' AND t.batch_id=c.batch_id
         AND t.source_report_event_key IS NULL
         AND NOT EXISTS (
           SELECT 1 FROM callback_batch_event recovery
           WHERE recovery.task_id=t.id
             AND recovery.recovered_from_task_id IS NOT NULL
         )
     )
  ON CONFLICT DO NOTHING
  RETURNING id,correlation_id,batch_id
), inserted_event AS (
  INSERT INTO callback_batch_event(
    task_id,batch_id,source_report_event_key,batch_no,biz_id,category,
    status,total,delivered,failed,unknown_cnt,manual_resolution_required,
    finished_at,legacy_unavailable
  )
  SELECT t.id,c.batch_id,CAST(:source_report_event_key AS char(64)),
    c.batch_no,c.biz_id,c.category,c.status,c.total,c.delivered,c.failed,
    c.unknown_cnt,c.status='completed_unknown',clock_timestamp(),false
  FROM inserted_task t
  JOIN candidate c ON c.batch_id=t.batch_id
  RETURNING task_id
)
SELECT t.id,t.correlation_id
FROM inserted_task t
JOIN inserted_event e ON e.task_id=t.id
"""


async def enqueue_batch_finished(
    connection: AsyncConnection,
    batch_id: int,
    *,
    source_report_event_key: str | None = None,
) -> None:
    """与批次状态更新同一事务写入终态任务和冻结汇总。"""

    correlation_id = current_correlation_id()
    assert correlation_id is not None
    inserted = await connection.execute(
        text(ENQUEUE_BATCH_FINISHED_SQL),
        {
            "batch_id": str(batch_id),
            "source_report_event_key": source_report_event_key,
            "correlation_id": correlation_id,
        },
    )
    row = inserted.mappings().one_or_none()
    if row is None:
        return
    task_id = int(row["id"])
    await enqueue_outbox(
        connection,
        OutboxEventSpec(
            event_type="callback.ready",
            aggregate_type="callback_task",
            aggregate_id=str(task_id),
            task_name="app.tasks.deliver_callback",
            queue="callback",
            args=(task_id,),
            dedup_key=f"callback:{task_id}:attempt:0",
            correlation_id=UUID(str(row["correlation_id"])),
        ),
    )


_PEEK_SQL = """
SELECT t.id,t.batch_id,t.event
FROM callback_task t
WHERE t.id=:task_id
"""

_LOCK_BATCH_SQL = """
SELECT id FROM sms_batch WHERE id=:batch_id FOR UPDATE
"""

_LOCK_SQL = """
SELECT t.id,t.batch_id,t.event,t.last_error
FROM callback_task t
WHERE t.id=:task_id
FOR UPDATE
"""

_STATE_SQL = """
SELECT legacy_unavailable
FROM callback_batch_event
WHERE task_id=:task_id
"""

_EXISTING_SQL = """
SELECT n.id,n.event_id
FROM callback_batch_event e
JOIN callback_task n ON n.id=e.task_id
WHERE e.recovered_from_task_id=:task_id
"""

_RECOVER_SQL = f"""
WITH candidate AS (
  SELECT b.id batch_id,a.id app_id,a.callback_url,a.callback_secret_enc,
    trim(b.batch_no) batch_no,b.biz_id,b.category,b.status,
    b.total,b.delivered,b.failed,b.unknown_cnt
  FROM sms_batch b
  JOIN app a ON a.id=b.app_id
  WHERE b.id=:batch_id
    AND b.status IN ({_TERMINAL})
    AND a.status=1 AND a.callback_url IS NOT NULL
    AND a.callback_secret_enc IS NOT NULL
), inserted_task AS (
  INSERT INTO callback_task(
    correlation_id,app_id,event,batch_id,source_report_event_key,url,
    callback_secret_enc,callback_secret_key_version,signature_version
  )
  SELECT :correlation_id,c.app_id,'batch.finished',c.batch_id,
    CAST(NULL AS char(64)),c.callback_url,c.callback_secret_enc,
    ((get_byte(c.callback_secret_enc,0) << 8)
      + get_byte(c.callback_secret_enc,1))::smallint,
    1
  FROM candidate c
  RETURNING id,event_id,correlation_id,batch_id
), inserted_event AS (
  INSERT INTO callback_batch_event(
    task_id,batch_id,source_report_event_key,recovered_from_task_id,
    batch_no,biz_id,category,status,total,delivered,failed,unknown_cnt,
    manual_resolution_required,finished_at,legacy_unavailable
  )
  SELECT t.id,c.batch_id,CAST(NULL AS char(64)),:source_task_id,
    c.batch_no,c.biz_id,c.category,c.status,c.total,c.delivered,c.failed,
    c.unknown_cnt,c.status='completed_unknown',clock_timestamp(),false
  FROM inserted_task t
  JOIN candidate c ON c.batch_id=t.batch_id
  RETURNING task_id
)
SELECT t.id,t.event_id,t.correlation_id
FROM inserted_task t
JOIN inserted_event e ON e.task_id=t.id
"""

_CLASSIFY_SQL = """
SELECT b.status batch_status,a.status app_status,
  a.callback_url,a.callback_secret_enc
FROM sms_batch b
JOIN app a ON a.id=b.app_id
WHERE b.id=:batch_id
"""


class FinishedRecovery:
    """补发结果。created 为假表示返回已经存在的新事件。"""

    def __init__(self, task_id: int, event_id: UUID, created: bool) -> None:
        self.task_id = task_id
        self.event_id = event_id
        self.created = created


class FinishedRecoveryService:
    """管理员把缺少快照的旧终态补成一条新 event_id。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def _engine(self) -> ManagedAsyncEngine:
        settings = self._settings or get_settings()
        return database_engine(settings.database_url_for("callback"))

    async def recover(
        self,
        task_id: int,
        *,
        principal: SecurityPrincipal,
    ) -> FinishedRecovery:
        """先锁批次再锁原任务。已有补发则原样返回，不改旧快照。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                return await self._recover(connection, task_id, principal)
        finally:
            await engine.dispose()

    async def _recover(
        self,
        connection: AsyncConnection,
        task_id: int,
        principal: SecurityPrincipal,
    ) -> FinishedRecovery:
        peeked = await connection.execute(text(_PEEK_SQL), {"task_id": task_id})
        task = peeked.mappings().one_or_none()
        if task is None:
            raise FinishedRecoveryRejected("回调任务不存在", "not_found", missing=True)
        if task["event"] != "batch.finished":
            raise FinishedRecoveryRejected("只有批次终态可以补发汇总", "not_legacy")
        if task["batch_id"] is None:
            raise FinishedRecoveryRejected("批次尚未终态，不能补发汇总", "batch_not_terminal")
        batch_id = int(task["batch_id"])
        locked_batch = await connection.execute(
            text(_LOCK_BATCH_SQL),
            {"batch_id": batch_id},
        )
        if locked_batch.mappings().one_or_none() is None:
            raise FinishedRecoveryRejected("批次尚未终态，不能补发汇总", "batch_not_terminal")
        locked = await connection.execute(text(_LOCK_SQL), {"task_id": task_id})
        task = locked.mappings().one_or_none()
        if task is None:
            raise FinishedRecoveryRejected("回调任务不存在", "not_found", missing=True)
        if task["event"] != "batch.finished" or task["batch_id"] is None:
            raise FinishedRecoveryRejected("只有批次终态可以补发汇总", "not_legacy")
        if task["last_error"] == "CallbackConfigRevoked":
            raise FinishedRecoveryRejected(
                "旧回调已因配置变更永久撤销，不能由它补发新事件",
                "config_revoked",
            )
        state = await connection.execute(text(_STATE_SQL), {"task_id": task_id})
        snapshot = state.mappings().one_or_none()
        if snapshot is None or snapshot["legacy_unavailable"] is not True:
            raise FinishedRecoveryRejected(
                "这条批次终态已有冻结汇总，不能另补一条",
                "not_legacy",
            )
        existing = await connection.execute(text(_EXISTING_SQL), {"task_id": task_id})
        found = existing.mappings().one_or_none()
        if found is not None:
            recovery = FinishedRecovery(
                int(found["id"]),
                UUID(str(found["event_id"])),
                False,
            )
            await _audit(connection, principal, task_id, recovery)
            return recovery
        correlation_id = current_correlation_id()
        assert correlation_id is not None
        inserted = await connection.execute(
            text(_RECOVER_SQL),
            {
                "batch_id": int(task["batch_id"]),
                "source_task_id": task_id,
                "correlation_id": correlation_id,
            },
        )
        created_row = inserted.mappings().one_or_none()
        if created_row is None:
            await _reject_unfinished(connection, int(task["batch_id"]))
        assert created_row is not None
        new_id = int(created_row["id"])
        await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="callback.ready",
                aggregate_type="callback_task",
                aggregate_id=str(new_id),
                task_name="app.tasks.deliver_callback",
                queue="callback",
                args=(new_id,),
                dedup_key=f"callback:{new_id}:attempt:0",
                correlation_id=UUID(str(created_row["correlation_id"])),
            ),
        )
        recovery = FinishedRecovery(new_id, UUID(str(created_row["event_id"])), True)
        await _audit(connection, principal, task_id, recovery)
        return recovery


async def _reject_unfinished(connection: AsyncConnection, batch_id: int) -> NoReturn:
    result = await connection.execute(text(_CLASSIFY_SQL), {"batch_id": batch_id})
    row = result.mappings().one_or_none()
    terminal = {
        "completed",
        "completed_unknown",
        "cancelled",
        "expired",
        "rejected",
    }
    if row is not None and str(row["batch_status"]) in terminal:
        secret = row["callback_secret_enc"]
        if int(row["app_status"]) != 1 or row["callback_url"] is None or secret is None:
            raise FinishedRecoveryRejected(
                "应用未启用或回调配置不完整，不能补发",
                "app_unavailable",
            )
    raise FinishedRecoveryRejected("批次尚未终态，不能补发汇总", "batch_not_terminal")


async def _audit(
    connection: AsyncConnection,
    principal: SecurityPrincipal,
    source_task_id: int,
    recovery: FinishedRecovery,
) -> None:
    await insert_audit(
        connection,
        AuditEvent(
            principal=principal,
            role=principal.role,
            action="callback_finished_recover",
            object_type="callback_task",
            object_id=str(recovery.task_id),
            after={
                "source_task_id": source_task_id,
                "event_id": str(recovery.event_id),
                "created": recovery.created,
            },
        ),
    )
