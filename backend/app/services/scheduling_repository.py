"""定时批次 PostgreSQL 原子领取与状态迁移。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.batch_query import BatchAccessScope
from app.services.callback_repository import enqueue_batch_finished
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.scheduling import ScheduledBatch
from app.services.usage_ledger import request_usage_release_for_batch
from app.settings import Settings, get_settings


class SqlSchedulingRepository:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        pooled: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.pooled = pooled

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def _dispose_task_engine(self, engine: Any) -> None:
        if not self.pooled:
            await engine.dispose()

    async def claim_due(self) -> list[ScheduledBatch]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_batch SET status='queued',updated_at=now()
                        WHERE id IN (
                          SELECT id FROM sms_batch WHERE status='scheduled'
                            AND scheduled_at<=now() ORDER BY scheduled_at
                          FOR UPDATE SKIP LOCKED LIMIT 100
                        ) RETURNING trim(batch_no) batch_no,category
                        """
                    )
                )
                rows = list(result.mappings())
                batches = [
                    ScheduledBatch(
                        str(row["batch_no"]),
                        str(row["category"]),
                        outbox_persisted=True,
                    )
                    for row in rows
                ]
                for batch in batches:
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="batch.ready",
                            aggregate_type="sms_batch",
                            aggregate_id=batch.batch_no,
                            task_name="app.tasks.send.process_batch",
                            queue=("bulk" if batch.category == "market" else "realtime"),
                            args=(batch.batch_no,),
                            dedup_key=f"scheduled:{batch.batch_no}:ready",
                        ),
                    )
                return batches
        finally:
            await self._dispose_task_engine(engine)

    async def cancel(
        self,
        batch_no: str,
        scope: BatchAccessScope,
    ) -> ScheduledBatch | None:
        predicate, scope_params = scope.sql()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE sms_batch b SET status='cancelled',updated_at=now()
                        WHERE b.batch_no=:batch_no AND b.status='scheduled' AND {predicate}
                        RETURNING b.id,trim(b.batch_no) batch_no,b.category,b.app_id,b.dept,
                          to_char(b.created_at AT TIME ZONE 'Asia/Shanghai','YYYYMMDD') quota_date,
                          b.quota_cost
                        """
                    ),
                    {"batch_no": batch_no, **scope_params},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                        VALUES(:actor,'batch_cancel','batch',:batch_no,
                          jsonb_build_object('status','cancelled'))
                        """
                    ),
                    {
                        "actor": self._actor(scope),
                        "batch_no": str(row["batch_no"]),
                    },
                )
                await enqueue_batch_finished(connection, int(row["id"]))
                batch = ScheduledBatch(
                    str(row["batch_no"]),
                    str(row["category"]),
                    int(row["app_id"] or 0),
                    str(row["dept"]),
                    str(row["quota_date"]),
                    int(row["quota_cost"]),
                    True,
                )
                release_event = f"batch:{batch.batch_no}:cancelled"
                if not await request_usage_release_for_batch(
                    connection,
                    batch_id=int(row["id"]),
                    event_id=release_event,
                ):
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="quota.compensation",
                            aggregate_type="sms_batch",
                            aggregate_id=batch.batch_no,
                            task_name="app.tasks.outbox.compensate_quota",
                            queue="realtime",
                            args=(
                                batch.app_id,
                                batch.dept,
                                batch.category,
                                batch.quota_date,
                                batch.quota_cost,
                                release_event,
                            ),
                            dedup_key=release_event,
                        ),
                    )
                return batch
        finally:
            await self._dispose_task_engine(engine)

    async def reschedule(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        scheduled_at: datetime,
        *,
        approval_expire_hours: int,
    ) -> bool:
        predicate, scope_params = scope.sql()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                selected = await connection.execute(
                    text(
                        f"""
                        SELECT b.id,b.channel,EXISTS(
                          SELECT 1 FROM approval p WHERE p.batch_id=b.id
                        ) had_approval FROM sms_batch b
                        WHERE b.batch_no=:batch_no AND b.status='scheduled' AND {predicate}
                        FOR UPDATE
                        """
                    ),
                    {"batch_no": batch_no, **scope_params},
                )
                row = selected.mappings().one_or_none()
                if row is None:
                    return False
                needs_reapproval = row["channel"] == "web" and bool(row["had_approval"])
                status = "pending_approval" if needs_reapproval else "scheduled"
                await connection.execute(
                    text(
                        """
                        UPDATE sms_batch SET scheduled_at=:scheduled_at,status=:status,
                          updated_at=now() WHERE id=:id
                        """
                    ),
                    {"id": row["id"], "scheduled_at": scheduled_at, "status": status},
                )
                if needs_reapproval:
                    await connection.execute(
                        text(
                            """
                            UPDATE approval SET status='pending',approver=NULL,reason=NULL,
                              decided_at=NULL,
                              expires_at=now()+make_interval(hours=>:expire_hours)
                            WHERE batch_id=:id
                            """
                        ),
                        {"id": row["id"], "expire_hours": approval_expire_hours},
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                        VALUES(:actor,'batch_reschedule','batch',:batch_no,
                          jsonb_build_object('scheduled_at',:scheduled_at))
                        """
                    ),
                    {
                        "actor": self._actor(scope),
                        "batch_no": batch_no,
                        "scheduled_at": scheduled_at.isoformat(),
                    },
                )
                return True
        finally:
            await self._dispose_task_engine(engine)

    @staticmethod
    def _actor(scope: BatchAccessScope) -> str:
        if scope.app_id is not None:
            return f"app:{scope.app_id}"
        if scope.dept is not None:
            return f"dept:{scope.dept}"
        return "admin"
