"""uncertain 对账的 PostgreSQL raw 索引、状态迁移与 log-sink 告警。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.alert_repository import SqlAlertService
from app.services.callback_repository import enqueue_batch_finished
from app.services.uncertain import RawCandidate, UncertainChunk
from app.settings import Settings, get_settings


class SqlUncertainRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_uncertain(self) -> list[UncertainChunk]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT c.id,c.custom_id,
                          COALESCE(c.uncertain_since,b.created_at) uncertain_since
                        FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.status='uncertain'
                        ORDER BY COALESCE(c.uncertain_since,b.created_at)
                        """
                    )
                )
                return [
                    UncertainChunk(
                        int(row["id"]),
                        str(row["custom_id"]).strip(),
                        row["uncertain_since"],
                    )
                    for row in result.mappings()
                ]
        finally:
            await engine.dispose()

    async def raw_candidates(self, custom_id: str) -> list[RawCandidate]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,source,payload_enc,payload_sha256,key_version
                        FROM raw_vendor_log
                        WHERE custom_ids @> ARRAY[CAST(:custom_id AS text)]
                        ORDER BY fetched_at DESC
                        """
                    ),
                    {"custom_id": custom_id},
                )
                return [
                    RawCandidate(
                        int(row["id"]),
                        str(row["source"]),
                        bytes(row["payload_enc"]),
                        str(row["payload_sha256"]),
                        int(row["key_version"]),
                    )
                    for row in result.mappings()
                ]
        finally:
            await engine.dispose()

    async def resolve_submitted(self, chunk_id: int, task_id: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='submitted',vendor_task_id=:task_id,
                          submitted_at=COALESCE(submitted_at,now())
                        WHERE id=:id AND status='uncertain'
                        """
                    ),
                    {"id": chunk_id, "task_id": task_id},
                )
                if result.rowcount == 1:
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_message SET status='sent' WHERE chunk_id=:id
                              AND status='pending'
                            """
                        ),
                        {"id": chunk_id},
                    )
        finally:
            await engine.dispose()

    async def alert_overdue(self, chunk: UncertainChunk) -> None:
        await SqlAlertService(self.settings).emit(
            alert_type="uncertain_overdue",
            level="crit",
            title="发送结果未知超过24小时",
            detail={"chunk_id": chunk.chunk_id, "custom_id": chunk.custom_id},
            dedup_key=f"uncertain_overdue:{chunk.chunk_id}",
        )

    async def terminalize_unknown(self, chunk: UncertainChunk) -> None:
        """无证据到期后一次进入保守终态；不重发、不退还可能已发生的成本。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                locked = await connection.execute(
                    text(
                        """
                        SELECT c.id chunk_id,c.batch_id
                        FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.id=:chunk_id AND c.status='uncertain'
                        FOR UPDATE OF c, b
                        """
                    ),
                    {"chunk_id": chunk.chunk_id},
                )
                row = locked.mappings().one_or_none()
                if row is None:
                    return
                batch_id = int(row["batch_id"])
                moved = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk
                        SET status='unknown_terminal',
                            unknown_terminal_at=now(),
                            submitting_since=NULL
                        WHERE id=:chunk_id AND status='uncertain'
                        """
                    ),
                    {"chunk_id": chunk.chunk_id},
                )
                if moved.rowcount != 1:
                    return
                await connection.execute(
                    text(
                        """
                        UPDATE sms_message SET status='unknown'
                        WHERE chunk_id=:chunk_id
                          AND status IN ('pending','sent')
                        """
                    ),
                    {"chunk_id": chunk.chunk_id},
                )
                aggregate = await connection.execute(
                    text(
                        """
                        UPDATE sms_batch b SET
                          delivered=s.delivered,failed=s.failed,
                          unknown_cnt=s.unknown_cnt,
                          status=CASE
                            WHEN b.status='completed_unknown' THEN 'completed_unknown'
                            WHEN s.active=0 AND s.unknown_cnt>0 THEN 'completed_unknown'
                            WHEN s.active=0 THEN 'completed'
                            ELSE b.status
                          END,
                          updated_at=now()
                        FROM (
                          SELECT batch_id,
                            count(*) FILTER (WHERE status='delivered') delivered,
                            count(*) FILTER (WHERE status='failed') failed,
                            count(*) FILTER (WHERE status='unknown') unknown_cnt,
                            count(*) FILTER (WHERE status IN ('pending','sent')) active
                          FROM sms_message WHERE batch_id=:batch_id GROUP BY batch_id
                        ) s
                        WHERE b.id=s.batch_id
                        RETURNING b.id,b.status,b.unknown_cnt
                        """
                    ),
                    {"batch_id": batch_id},
                )
                batch = aggregate.mappings().one()
                if str(batch["status"]) in {"completed", "completed_unknown"}:
                    await enqueue_batch_finished(connection, int(batch["id"]))
                unknown_count = int(batch["unknown_cnt"])
            await SqlAlertService(self.settings).emit(
                alert_type="uncertain_terminal",
                level="info",
                title="uncertain 已进入保守终态",
                detail={
                    "chunk_id": chunk.chunk_id,
                    "batch_id": batch_id,
                    "unknown_count": unknown_count,
                },
                dedup_key=f"uncertain_terminal:{chunk.chunk_id}",
            )
        finally:
            await engine.dispose()
