"""运维中心只读安全元数据的 PostgreSQL 仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import text

from app.core.jobtrack import JobSpec
from app.core.runtime_resources import database_engine
from app.services.ops import (
    AlertLevel,
    AlertQuery,
    AlertRecord,
    JobRecord,
    OpsPage,
    PausedBatch,
    QueueSnapshot,
    RawLogQuery,
    RawLogRecord,
    RawSource,
    UncertainRecord,
    UnmatchedQuery,
    UnmatchedRecord,
)
from app.services.raw_replay import (
    MAX_RAW_REPLAY_ATTEMPTS,
    RawReplayClaim,
    RawReplayRecord,
)
from app.settings import Settings, get_settings


class SqlOpsRepository:
    """查询绝不选择 raw 密文或 unmatched 号码保护字段。"""

    def __init__(self, settings: Settings | None = None, redis: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis: Any = redis or Redis.from_url(
            self.settings.redis_control_url,
            decode_responses=True,
        )

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_jobs(
        self,
        specs: Sequence[JobSpec],
        *,
        now: datetime,
    ) -> tuple[JobRecord, ...]:
        if not specs:
            return ()
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH specs AS (
                          SELECT * FROM unnest(
                            CAST(:job_names AS text[]),CAST(:intervals AS integer[])
                          ) AS value(job_name,expect_interval_s)
                        ), latest AS (
                          SELECT DISTINCT ON (job_name) job_name,started_at,status,
                            duration_ms,items
                          FROM job_run ORDER BY job_name,started_at DESC,id DESC
                        ), stats AS (
                          SELECT job_name,
                            count(*) FILTER (WHERE status='success') success_count,
                            count(*) FILTER (WHERE status='failed') failed_count
                          FROM job_run
                          WHERE started_at>=CAST(:day_start AS timestamptz)
                            AND status IN ('success','failed')
                          GROUP BY job_name
                        )
                        SELECT s.job_name,l.started_at last_run_at,
                          l.status last_status,l.duration_ms last_duration_ms,
                          COALESCE(l.items,0) last_items,
                          COALESCE(
                            st.success_count::double precision /
                            NULLIF(st.success_count+st.failed_count,0),0
                          ) success_rate_24h,
                          (l.started_at IS NULL OR l.started_at < CAST(:now AS timestamptz) -
                            make_interval(secs=>s.expect_interval_s*2)) stalled
                        FROM specs s LEFT JOIN latest l USING(job_name)
                        LEFT JOIN stats st USING(job_name)
                        ORDER BY s.job_name
                        """
                    ),
                    {
                        "job_names": [spec.job_name for spec in specs],
                        "intervals": [spec.expect_interval_s for spec in specs],
                        "now": now,
                        "day_start": now - timedelta(hours=24),
                    },
                )
                return tuple(
                    JobRecord(
                        str(row["job_name"]),
                        row["last_run_at"],
                        str(row["last_status"]) if row["last_status"] is not None else None,
                        int(row["last_duration_ms"])
                        if row["last_duration_ms"] is not None
                        else None,
                        int(row["last_items"]),
                        float(row["success_rate_24h"]),
                        bool(row["stalled"]),
                    )
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def audit_job_trigger(
        self,
        job_name: str,
        *,
        actor: str,
        ip: str,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'job_trigger','job',:job_name,
                          jsonb_build_object('status','requested')
                        )
                        """
                    ),
                    {"actor": actor, "ip": ip, "job_name": job_name},
                )
        finally:
            await engine.dispose()

    async def queue_snapshot(self) -> QueueSnapshot:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT balance FROM balance_snapshot
                           ORDER BY fetched_at DESC,id DESC LIMIT 1) balance,
                          COALESCE((SELECT value::integer FROM sys_config
                            WHERE key='balance_alert_threshold'),10000) threshold
                        """
                    )
                )
                row = result.mappings().one()
        finally:
            await engine.dispose()
        values = await self.redis.mget(
            "queue:paused:realtime",
            "queue:paused:bulk",
        )
        return QueueSnapshot(
            str(values[0]) if values[0] is not None else None,
            str(values[1]) if values[1] is not None else None,
            int(row["balance"]) if row["balance"] is not None else None,
            int(row["threshold"]),
        )

    async def resume_batches(
        self,
        *,
        actor: str,
        ip: str,
    ) -> tuple[PausedBatch, ...]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_batch SET status='queued',updated_at=now()
                        WHERE status='balance_blocked'
                        RETURNING trim(batch_no) batch_no,category
                        """
                    )
                )
                batches = tuple(
                    PausedBatch(str(row["batch_no"]), str(row["category"]))
                    for row in result.mappings()
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'queue_resume','queue','all',
                          jsonb_build_object('resumed_batches',CAST(:count AS integer))
                        )
                        """
                    ),
                    {"actor": actor, "ip": ip, "count": len(batches)},
                )
                return batches
        finally:
            await engine.dispose()

    async def clear_queue_pauses(self) -> None:
        await self.redis.delete("queue:paused:realtime", "queue:paused:bulk")

    async def list_stale_unprocessed_raw_ids(
        self,
        *,
        limit: int = 20,
        max_attempts: int = MAX_RAW_REPLAY_ATTEMPTS,
    ) -> list[int]:
        """列出租约过期或尚未开始处理的未处理 raw，供自动重放。

        尝试次数少者优先，防止最老的永久毒丸垄断整个 LIMIT 窗口；达到
        上限的 raw 退出自动重放，仅保留人工重放入口。
        """

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id FROM raw_vendor_log
                        WHERE processed=false
                          AND replay_attempts<:max_attempts
                          AND (
                            processing_started_at IS NULL
                            OR processing_started_at<=now()-interval '15 minutes'
                          )
                        ORDER BY replay_attempts,id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit, "max_attempts": max_attempts},
                )
                return [int(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def claim_raw_for_replay(self, raw_id: int) -> RawReplayClaim | None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH claimed AS (
                          UPDATE raw_vendor_log
                          SET processing_started_at=now(),error=NULL,
                            replay_attempts=replay_attempts+1
                          WHERE id=:raw_id AND processed=false
                            AND (
                              processing_started_at IS NULL
                              OR processing_started_at<=now()-interval '15 minutes'
                            )
                          RETURNING id,source,payload_enc,payload_sha256,
                            key_version,processed,http_status,content_encoding
                        )
                        SELECT id,source,payload_enc,payload_sha256,key_version,
                          processed,http_status,content_encoding,true claimed
                        FROM claimed
                        UNION ALL
                        SELECT id,source,payload_enc,payload_sha256,key_version,
                          processed,http_status,content_encoding,false claimed
                        FROM raw_vendor_log
                        WHERE id=:raw_id
                          AND NOT EXISTS (SELECT 1 FROM claimed)
                        LIMIT 1
                        """
                    ),
                    {"raw_id": raw_id},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                return RawReplayClaim(
                    record=RawReplayRecord(
                        int(row["id"]),
                        str(row["source"]),
                        bytes(row["payload_enc"]),
                        str(row["payload_sha256"]),
                        int(row["key_version"]),
                        bool(row["processed"]),
                        int(row["http_status"]),
                        str(row["content_encoding"]),
                    ),
                    claimed=bool(row["claimed"]),
                )
        finally:
            await engine.dispose()

    async def raw_replay_exhausted(
        self,
        raw_id: int,
        *,
        max_attempts: int = MAX_RAW_REPLAY_ATTEMPTS,
    ) -> bool:
        """该未处理 raw 是否已耗尽自动重放次数（告警一次并转人工）。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.scalar(
                    text(
                        """
                        SELECT replay_attempts>=:max_attempts FROM raw_vendor_log
                        WHERE id=:raw_id AND processed=false
                        """
                    ),
                    {"raw_id": raw_id, "max_attempts": max_attempts},
                )
                return bool(result)
        finally:
            await engine.dispose()

    async def mark_replay_error(self, raw_id: int, error: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE raw_vendor_log SET processed=false,error=:error,"
                        "processing_started_at=NULL "
                        "WHERE id=:raw_id AND processed=false"
                    ),
                    {"raw_id": raw_id, "error": error[:256]},
                )
        finally:
            await engine.dispose()

    async def audit_raw_replay(
        self,
        raw_id: int,
        *,
        source: str,
        items: int,
        actor: str,
        ip: str,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,role,ip,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'admin',CAST(:ip AS inet),'raw_replay','raw_vendor_log',
                          CAST(CAST(:raw_id AS bigint) AS text),jsonb_build_object(
                            'source',CAST(:source AS text),'items',CAST(:items AS integer)
                          )
                        )
                        """
                    ),
                    {
                        "actor": actor,
                        "ip": ip,
                        "raw_id": raw_id,
                        "source": source,
                        "items": items,
                    },
                )
        finally:
            await engine.dispose()

    async def list_alerts(self, query: AlertQuery) -> OpsPage[AlertRecord]:
        where = """
          (CAST(:alert_type AS varchar(32)) IS NULL OR alert_type=:alert_type)
          AND (CAST(:level AS varchar(8)) IS NULL OR level=:level)
          AND (CAST(:start AS timestamptz) IS NULL OR created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR created_at<=:end)
        """
        params = self._page_params(query.page, query.page_size) | {
            "alert_type": query.alert_type,
            "level": query.level,
            "start": query.start,
            "end": query.end,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM alert_log WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,alert_type,level,title,detail,channels,created_at "
                        "FROM alert_log WHERE "
                        + where
                        + " ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    AlertRecord(
                        int(row["id"]),
                        str(row["alert_type"]),
                        cast(AlertLevel, str(row["level"])),
                        str(row["title"]),
                        cast(dict[str, Any] | None, row["detail"]),
                        str(row["channels"]),
                        row["created_at"],
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    async def list_raw_logs(self, query: RawLogQuery) -> OpsPage[RawLogRecord]:
        where = """
          (CAST(:source AS varchar(8)) IS NULL OR source=:source)
          AND (CAST(:processed AS boolean) IS NULL OR processed=:processed)
        """
        params = self._page_params(query.page, query.page_size) | {
            "source": query.source,
            "processed": query.processed,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM raw_vendor_log WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,source,item_count,cardinality(custom_ids) custom_id_count,"
                        "processed,error,fetched_at FROM raw_vendor_log WHERE "
                        + where
                        + " ORDER BY fetched_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    RawLogRecord(
                        int(row["id"]),
                        cast(RawSource, str(row["source"])),
                        int(row["item_count"]),
                        int(row["custom_id_count"]),
                        bool(row["processed"]),
                        str(row["error"]) if row["error"] is not None else None,
                        row["fetched_at"],
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    async def list_uncertain(self, page: int, page_size: int) -> OpsPage[UncertainRecord]:
        params = self._page_params(page, page_size)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM sms_chunk WHERE status='uncertain'"), params
                )
                result = await connection.execute(
                    text(
                        """
                        SELECT c.id chunk_id,trim(b.batch_no) batch_no,
                          trim(c.custom_id) custom_id,c.phone_count,c.vendor_code,
                          COALESCE(c.uncertain_since,b.created_at) uncertain_since,
                          GREATEST(0,EXTRACT(EPOCH FROM (
                            now()-COALESCE(c.uncertain_since,b.created_at)
                          ))::bigint) age_seconds
                        FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.status='uncertain'
                        ORDER BY COALESCE(c.uncertain_since,b.created_at),c.id
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
                items = tuple(
                    UncertainRecord(
                        int(row["chunk_id"]),
                        str(row["batch_no"]),
                        str(row["custom_id"]),
                        int(row["phone_count"]),
                        int(row["vendor_code"]) if row["vendor_code"] is not None else None,
                        row["uncertain_since"],
                        int(row["age_seconds"]),
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), page, page_size)
        finally:
            await engine.dispose()

    async def list_unmatched(self, query: UnmatchedQuery) -> OpsPage[UnmatchedRecord]:
        where = """
          (:has_phone=false OR phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[])))
          AND (CAST(:start AS timestamptz) IS NULL OR created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR created_at<=:end)
        """
        params = self._page_params(query.page, query.page_size) | {
            "has_phone": bool(query.phone_hmacs),
            "phone_hmacs": list(query.phone_hmacs),
            "start": query.start,
            "end": query.end,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM unmatched_report WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,vendor_task_id,custom_id,phone_mask,report_status,"
                        "report_desc,report_time,created_at FROM unmatched_report WHERE "
                        + where
                        + " ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    UnmatchedRecord(
                        int(row["id"]),
                        str(row["vendor_task_id"]) if row["vendor_task_id"] is not None else None,
                        str(row["custom_id"]) if row["custom_id"] is not None else None,
                        str(row["phone_mask"]),
                        int(row["report_status"]) if row["report_status"] is not None else None,
                        str(row["report_desc"]) if row["report_desc"] is not None else None,
                        row["report_time"],
                        row["created_at"],
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    @staticmethod
    def _page_params(page: int, page_size: int) -> dict[str, object]:
        return {"limit": page_size, "offset": (page - 1) * page_size}
