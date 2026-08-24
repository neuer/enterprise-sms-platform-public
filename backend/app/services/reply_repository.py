"""上行回复 raw、关联与幂等持久化 PostgreSQL 仓储。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.raw_lease import (
    FENCED_METADATA_SQL,
    FENCED_TERMINAL_SQL,
    PERSIST_LEASE_COLUMNS,
    PERSIST_LEASE_VALUES,
    RawProcessingLease,
    commit_fenced_raw_update,
    new_lease_id,
    require_lease,
)
from app.services.raw_parse import (
    ELIGIBILITY_NEVER,
    PARSE_PROCESSED,
    mark_error_column_values,
    persist_column_values,
)
from app.services.reply_ingest import ProtectedReply
from app.settings import Settings, get_settings


class SqlReplyRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._leases: dict[int, RawProcessingLease] = {}

    def remember_lease(self, lease: RawProcessingLease) -> None:
        self._leases[lease.raw_id] = lease

    def _lease_for(self, raw_id: int, lease: RawProcessingLease | None) -> RawProcessingLease:
        return require_lease(lease or self._leases.get(raw_id), raw_id)

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def persist_raw(self, **values: Any) -> int:
        """独立事务提交完整 GetReply 密文，返回后才允许解析。"""

        payload = dict(values)
        payload["capture_state"] = payload.get("capture_state") or "complete"
        payload.update(
            persist_column_values(
                capture_state=str(payload["capture_state"]),
                http_status=payload.get("http_status"),
                content_encoding=str(payload.get("content_encoding") or "identity"),
            )
        )
        lease_id = new_lease_id()
        payload["processing_lease_id"] = str(lease_id)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,key_version,http_status,
                          content_encoding,custom_ids,item_count,processing_started_at,
                          capture_state,parse_state,replay_eligibility,
                          {PERSIST_LEASE_COLUMNS.strip()}
                        ) VALUES (
                          'reply',:payload_enc,:payload_sha256,:key_version,:http_status,
                          :content_encoding,
                          CAST(:custom_ids AS text[]),:item_count,now(),
                          COALESCE(:capture_state,'complete'),
                          COALESCE(:parse_state,'unattempted'),
                          COALESCE(:replay_eligibility,'manual'),
                          {PERSIST_LEASE_VALUES.strip()}
                        ) RETURNING id
                        """
                    ),
                    payload,
                )
                raw_id = int(result.scalar_one())
        finally:
            await engine.dispose()
        self.remember_lease(RawProcessingLease(raw_id, lease_id, 1))
        return raw_id

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
        lease: RawProcessingLease | None = None,
    ) -> None:
        """raw 已提交后补充不含 PII 的索引元数据。"""

        token = self._lease_for(raw_id, lease)
        engine = self._engine()
        try:
            await commit_fenced_raw_update(
                engine,
                FENCED_METADATA_SQL + " AND source='reply'",
                {
                    "id": raw_id,
                    "custom_ids": custom_ids,
                    "item_count": item_count,
                    "lease_id": str(token.lease_id),
                    "epoch": token.epoch,
                },
                lease=token,
            )
        finally:
            await engine.dispose()

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
        if not custom_ids:
            return []
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT trim(custom_id) FROM sms_chunk "
                        "WHERE trim(custom_id)=ANY(CAST(:custom_ids AS text[])) ORDER BY 1"
                    ),
                    {"custom_ids": custom_ids},
                )
                return [str(value) for value in result.scalars()]
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
                            event_key,event_key_version,raw_id,vendor_task_id,custom_id,
                            phone_enc,phone_hmac,phone_mask,key_version,
                            ext_code,content,content_enc,is_optout,reply_time
                          ) VALUES (
                            CAST(:dedup_hash AS char(64)),
                            CAST(:dedup_key_version AS smallint),:raw_id,
                            CAST(:vendor_task_id AS varchar(64)),
                            CAST(:custom_id AS varchar(64)),
                            CAST(:phone_enc AS bytea),CAST(:phone_hmac AS char(64)),
                            CAST(:phone_mask AS varchar(11)),
                            CAST(:key_version AS smallint),
                            CAST(:ext_code AS varchar(8)),
                            '[encrypted]',CAST(:content_enc AS bytea),
                            CAST(:is_optout AS boolean),
                            CAST(:reply_time AS timestamptz)
                          )
                          ON CONFLICT(event_key) DO NOTHING
                          RETURNING event_key
                        ), matched AS (
                          SELECT c.batch_id FROM sms_chunk c
                          JOIN sms_message m ON m.chunk_id=c.id
                          WHERE (
                            (CAST(:match_custom_id AS varchar(32)) IS NOT NULL
                              AND c.custom_id=CAST(:match_custom_id AS varchar(32)))
                            OR c.vendor_task_id=CAST(:vendor_task_id AS varchar(64))
                          )
                            AND m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                          ORDER BY CASE
                            WHEN c.custom_id=CAST(:match_custom_id AS varchar(32)) THEN 0 ELSE 1
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
                          '[encrypted]',CAST(:reply_time AS timestamptz)
                        FROM event_insert
                        """
                    ),
                    {
                        "raw_id": raw_id,
                        "vendor_task_id": reply.vendor_task_id,
                        "custom_id": reply.custom_id,
                        "match_custom_id": reply.match_custom_id,
                        "phone_enc": reply.phone_enc,
                        "phone_hmac": reply.phone_hmac,
                        "phone_hmacs": list(reply.phone_hmacs),
                        "phone_mask": reply.phone_mask,
                        "key_version": reply.key_version,
                        "ext_code": reply.ext_code,
                        "content_enc": reply.content_enc,
                        "is_optout": reply.is_optout,
                        "reply_time": reply.reply_time,
                        "dedup_hash": reply.dedup_hash,
                        "dedup_key_version": reply.dedup_key_version,
                    },
                )
        finally:
            await engine.dispose()

    async def mark_processed(
        self, raw_id: int, *, lease: RawProcessingLease | None = None
    ) -> None:
        await self._mark_raw(
            raw_id,
            processed=True,
            error=None,
            parse_state=PARSE_PROCESSED,
            replay_eligibility=ELIGIBILITY_NEVER,
            lease=lease,
        )

    async def mark_error(
        self, raw_id: int, error: str, *, lease: RawProcessingLease | None = None
    ) -> None:
        message = error[:256]
        columns = mark_error_column_values(message)
        await self._mark_raw(
            raw_id,
            processed=False,
            error=message,
            parse_state=columns["parse_state"],
            replay_eligibility=columns["replay_eligibility"],
            lease=lease,
        )

    async def _mark_raw(
        self,
        raw_id: int,
        *,
        processed: bool,
        error: str | None,
        parse_state: str,
        replay_eligibility: str,
        lease: RawProcessingLease | None = None,
    ) -> None:
        token = self._lease_for(raw_id, lease)
        engine = self._engine()
        try:
            await commit_fenced_raw_update(
                engine,
                FENCED_TERMINAL_SQL + " AND source='reply'",
                {
                    "id": raw_id,
                    "processed": processed,
                    "error": error,
                    "parse_state": parse_state,
                    "replay_eligibility": replay_eligibility,
                    "lease_id": str(token.lease_id),
                    "epoch": token.epoch,
                },
                lease=token,
            )
        finally:
            await engine.dispose()
        self._leases.pop(raw_id, None)
