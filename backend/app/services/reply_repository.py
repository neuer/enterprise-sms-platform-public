"""上行回复 raw、关联与幂等持久化 PostgreSQL 仓储。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.reply_ingest import ProtectedReply
from app.settings import Settings, get_settings


class SqlReplyRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def persist_raw(self, **values: Any) -> int:
        """独立事务提交完整 GetReply 密文，返回后才允许解析。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,key_version,custom_ids,
                          item_count,processing_started_at
                        ) VALUES (
                          'reply',:payload_enc,:payload_sha256,:key_version,
                          CAST(:custom_ids AS text[]),:item_count,now()
                        ) RETURNING id
                        """
                    ),
                    values,
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    async def store_reply(self, raw_id: int, reply: ProtectedReply) -> None:
        if raw_id < 1:
            raise ValueError("raw_id must be positive")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        WITH event_insert AS (
                          INSERT INTO reply_event(
                            event_key,raw_id,vendor_task_id,custom_id,
                            phone_enc,phone_hmac,phone_mask,key_version,
                            ext_code,content,reply_time
                          ) VALUES (
                            CAST(:dedup_hash AS char(64)),:raw_id,
                            CAST(:vendor_task_id AS varchar(64)),
                            CAST(:custom_id AS varchar(64)),
                            CAST(:phone_enc AS bytea),CAST(:phone_hmac AS char(64)),
                            CAST(:phone_mask AS varchar(11)),
                            CAST(:key_version AS smallint),
                            CAST(:ext_code AS varchar(8)),
                            CAST(:content AS varchar(500)),
                            CAST(:reply_time AS timestamptz)
                          )
                          ON CONFLICT(event_key) DO NOTHING
                          RETURNING event_key
                        ), matched AS (
                          SELECT c.batch_id FROM sms_chunk c
                          WHERE (CAST(:custom_id AS varchar(64)) IS NOT NULL
                            AND c.custom_id=CAST(:custom_id AS varchar(64)))
                             OR c.vendor_task_id=CAST(:vendor_task_id AS varchar(64))
                          ORDER BY CASE
                            WHEN c.custom_id=CAST(:custom_id AS varchar(64)) THEN 0 ELSE 1
                          END,
                                   c.id DESC
                          LIMIT 1
                        )
                        INSERT INTO sms_reply(
                          event_key,vendor_task_id,batch_id,
                          phone_enc,phone_hmac,phone_mask,key_version,
                          ext_code,content,reply_time
                        )
                        SELECT event_insert.event_key,
                          CAST(:vendor_task_id AS varchar(64)),
                          (SELECT batch_id FROM matched),CAST(:phone_enc AS bytea),
                          CAST(:phone_hmac AS char(64)),CAST(:phone_mask AS varchar(11)),
                          CAST(:key_version AS smallint),CAST(:ext_code AS varchar(8)),
                          CAST(:content AS varchar(500)),CAST(:reply_time AS timestamptz)
                        FROM event_insert
                        """
                    ),
                    {
                        "raw_id": raw_id,
                        "vendor_task_id": reply.vendor_task_id,
                        "custom_id": reply.custom_id,
                        "phone_enc": reply.phone_enc,
                        "phone_hmac": reply.phone_hmac,
                        "phone_mask": reply.phone_mask,
                        "key_version": reply.key_version,
                        "ext_code": reply.ext_code,
                        "content": reply.content,
                        "reply_time": reply.reply_time,
                        "dedup_hash": reply.dedup_hash,
                    },
                )
        finally:
            await engine.dispose()

    async def mark_processed(self, raw_id: int) -> None:
        await self._mark_raw(raw_id, processed=True, error=None)

    async def mark_error(self, raw_id: int, error: str) -> None:
        await self._mark_raw(raw_id, processed=False, error=error[:256])

    async def _mark_raw(self, raw_id: int, *, processed: bool, error: str | None) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE raw_vendor_log SET processed=:processed,error=:error,"
                        "processing_started_at=NULL "
                        "WHERE id=:id AND source='reply'"
                    ),
                    {"id": raw_id, "processed": processed, "error": error},
                )
        finally:
            await engine.dispose()
