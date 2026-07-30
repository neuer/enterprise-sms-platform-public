"""uncertain 对账的 PostgreSQL raw 索引、状态迁移与 log-sink 告警。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.alert_repository import SqlAlertService
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
