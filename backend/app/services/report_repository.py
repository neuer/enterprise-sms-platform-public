"""状态报告 raw、消息回写与 unmatched 的 PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import database_engine
from app.services.callback_repository import (
    enqueue_batch_finished,
    enqueue_message_report,
)
from app.services.raw_lease import (
    FENCED_METADATA_SQL,
    PERSIST_LEASE_COLUMNS,
    PERSIST_LEASE_VALUES,
    PERSIST_STARTED_AT_SQL,
    RAW_LEASE_SECONDS,
    SYSTEM_REPLAY_AUDIT_PENDING,
    RawProcessingLease,
    commit_fenced_raw_update,
    fenced_terminal_sql,
    new_lease_id,
    renew_raw_lease,
    require_lease,
)
from app.services.raw_parse import (
    ELIGIBILITY_NEVER,
    PARSE_PROCESSED,
    mark_error_column_values,
    persist_column_values,
)
from app.services.report_ingest import (
    FailureRateAlert,
    ProtectedReport,
    ReportApplyResult,
)
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)


class SqlReportRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._leases: dict[int, RawProcessingLease] = {}

    def remember_lease(self, lease: RawProcessingLease) -> None:
        self._leases[lease.raw_id] = lease

    def _lease_for(self, raw_id: int, lease: RawProcessingLease | None) -> RawProcessingLease:
        return require_lease(lease or self._leases.get(raw_id), raw_id)

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def report_timeout_hours(self) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='report_timeout_hours'")
                )
                return int(result.scalar_one_or_none() or 48)
        finally:
            await engine.dispose()

    async def persist_raw(self, **values: Any) -> int:
        """独立事务提交完整 raw 密文，返回后业务解析才可开始。"""

        payload = dict(values)
        acquire = bool(payload.pop("acquire_processing_lease", True))
        payload["capture_state"] = payload.get("capture_state") or "complete"
        payload.update(
            persist_column_values(
                capture_state=str(payload["capture_state"]),
                http_status=payload.get("http_status"),
                content_encoding=str(payload.get("content_encoding") or "identity"),
            )
        )
        lease_id = new_lease_id() if acquire else None
        payload["acquire_processing_lease"] = acquire
        payload["processing_lease_id"] = str(lease_id) if lease_id is not None else None
        payload["lease_seconds"] = RAW_LEASE_SECONDS
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        INSERT INTO raw_vendor_log (
                          source,payload_enc,payload_sha256,key_version,http_status,
                          content_encoding,custom_ids,item_count,processing_started_at,
                          capture_state,parse_state,replay_eligibility,
                          {PERSIST_LEASE_COLUMNS.strip()}
                        ) VALUES (
                          'report',:payload_enc,:payload_sha256,:key_version,:http_status,
                          :content_encoding,
                          CAST(:custom_ids AS text[]),:item_count,
                          {PERSIST_STARTED_AT_SQL.strip()},
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
        if lease_id is not None:
            self.remember_lease(RawProcessingLease(raw_id, lease_id, 1))
        return raw_id

    async def renew_processing_lease(self, lease: RawProcessingLease) -> None:
        engine = self._engine()
        try:
            await renew_raw_lease(engine, lease)
        finally:
            await engine.dispose()

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
                FENCED_METADATA_SQL + " AND source='report'",
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

    @staticmethod
    async def _lock_batch(connection: AsyncConnection, batch_id: int) -> None:
        await connection.execute(
            text("SELECT id FROM sms_batch WHERE id=:batch_id FOR UPDATE"),
            {"batch_id": batch_id},
        )

    @staticmethod
    async def _persist_event(
        connection: AsyncConnection,
        raw_id: int,
        report: ProtectedReport,
    ) -> None:
        """以数据库主键幂等写入不可变报告事实。"""

        await connection.execute(
            text(
                """
                INSERT INTO report_event(
                  event_key,raw_id,vendor_task_id,custom_id,
                  phone_enc,phone_hmac,phone_mask,key_version,
                  report_status,message_status,report_desc,report_time
                ) VALUES (
                  CAST(:event_key AS char(64)),:raw_id,
                  CAST(:vendor_task_id AS varchar(64)),
                  CAST(:custom_id AS varchar(64)),
                  :phone_enc,CAST(:phone_hmac AS char(64)),:phone_mask,:key_version,
                  CAST(:report_status AS smallint),:message_status,
                  :report_desc,:report_time
                )
                ON CONFLICT(event_key) DO NOTHING
                """
            ),
            {
                "event_key": report.event_key,
                "raw_id": raw_id,
                "vendor_task_id": report.vendor_task_id,
                "custom_id": report.custom_id,
                "phone_enc": report.phone_enc,
                "phone_hmac": report.phone_hmac,
                "phone_mask": report.phone_mask,
                "key_version": report.key_version,
                "report_status": report.report_status,
                "message_status": report.message_status,
                "report_desc": report.report_desc,
                "report_time": report.report_time,
            },
        )

    @classmethod
    async def _refresh_batch(
        cls,
        connection: AsyncConnection,
        batch_id: int,
        *,
        batch_locked: bool = False,
        source_report_event_key: str | None = None,
    ) -> None:
        if not batch_locked:
            await cls._lock_batch(connection, batch_id)
        await connection.execute(
            text(
                """
                UPDATE sms_batch b SET
                  delivered=s.delivered, failed=s.failed, unknown_cnt=s.unknown_cnt,
                  status=CASE WHEN s.active=0 THEN 'completed' ELSE b.status END,
                  updated_at=now()
                FROM (
                  SELECT batch_id,
                    count(*) FILTER (WHERE status='delivered') delivered,
                    count(*) FILTER (WHERE status='failed') failed,
                    count(*) FILTER (WHERE status='unknown') unknown_cnt,
                    count(*) FILTER (WHERE status IN ('pending','sent')) active
                  FROM sms_message WHERE batch_id=:batch_id GROUP BY batch_id
                ) s WHERE b.id=s.batch_id
                """
            ),
            {"batch_id": batch_id},
        )
        await enqueue_batch_finished(
            connection,
            batch_id,
            source_report_event_key=source_report_event_key,
        )

    async def apply_report(
        self,
        raw_id: int,
        report: ProtectedReport,
    ) -> ReportApplyResult | None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._persist_event(connection, raw_id, report)
                result = await connection.execute(
                    text(
                        """
                        SELECT m.id,m.created_at,m.batch_id FROM sms_chunk c
                        JOIN sms_message m ON m.chunk_id=c.id
                        WHERE c.custom_id=:match_custom_id
                          AND m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                        ORDER BY m.created_at DESC LIMIT 2
                        """
                    ),
                    {
                        "match_custom_id": report.match_custom_id,
                        "phone_hmacs": list(report.phone_hmacs),
                    },
                )
                matches = list(result.mappings())
                if not matches:
                    return None
                if len(matches) > 1:
                    LOGGER.warning(
                        "ambiguous report match skipped",
                        extra={"custom_id_len": len(report.match_custom_id)},
                    )
                    return None
                row = matches[0]
                batch_id = int(row["batch_id"])
                await self._lock_batch(connection, batch_id)
                locked_message = await connection.execute(
                    text(
                        """
                        SELECT id,created_at,batch_id FROM sms_message m
                        WHERE id=:id AND created_at=:created_at AND batch_id=:batch_id
                        FOR UPDATE OF m
                        """
                    ),
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "batch_id": batch_id,
                    },
                )
                row = locked_message.mappings().one_or_none()
                if row is None:
                    return None
                projection = await connection.execute(
                    text(
                        """
                        INSERT INTO report_event_projection(
                          event_key,batch_id,message_id,message_created_at,
                          projection_changed
                        ) VALUES (
                          CAST(:event_key AS char(64)),:batch_id,:message_id,
                          :message_created_at,false
                        )
                        ON CONFLICT(event_key) DO NOTHING
                        RETURNING event_key
                        """
                    ),
                    {
                        "event_key": report.event_key,
                        "batch_id": batch_id,
                        "message_id": row["id"],
                        "message_created_at": row["created_at"],
                    },
                )
                if projection.scalar_one_or_none() is None:
                    return ReportApplyResult(batch_id, changed=False)
                updated = await connection.execute(
                    text(
                        """
                        UPDATE sms_message m SET status=:status,
                          report_status=:report_status,report_desc=:report_desc,
                          report_time=:report_time,
                          report_event_key=CAST(:event_key AS char(64))
                        WHERE m.id=:id AND m.created_at=:created_at
                          AND (
                            m.report_status IS DISTINCT FROM 1
                            OR CAST(:report_status AS smallint) NOT IN (2, 99)
                          )
                          AND (
                            m.report_time IS NULL
                            OR (
                              CASE
                                WHEN CAST(:report_status AS smallint)=1 THEN 4
                                WHEN CAST(:report_status AS smallint) IN (2,99)
                                  THEN 3
                                WHEN CAST(:report_status AS smallint)=0 THEN 1
                                ELSE 2
                              END
                              >
                              CASE
                                WHEN m.report_status=1 THEN 4
                                WHEN m.report_status IN (2,99) THEN 3
                                WHEN m.report_status=0 THEN 1
                                WHEN m.report_status IS NULL THEN 0
                                ELSE 2
                              END
                            )
                            OR (
                              CASE
                                WHEN CAST(:report_status AS smallint)=1 THEN 4
                                WHEN CAST(:report_status AS smallint) IN (2,99)
                                  THEN 3
                                WHEN CAST(:report_status AS smallint)=0 THEN 1
                                ELSE 2
                              END
                              =
                              CASE
                                WHEN m.report_status=1 THEN 4
                                WHEN m.report_status IN (2,99) THEN 3
                                WHEN m.report_status=0 THEN 1
                                WHEN m.report_status IS NULL THEN 0
                                ELSE 2
                              END
                              AND (
                                CAST(:report_time AS timestamptz)>m.report_time
                                OR (
                                  CAST(:report_time AS timestamptz)=m.report_time
                                  AND (
                                    CASE CAST(:report_status AS smallint)
                                      WHEN 1 THEN 40
                                      WHEN 99 THEN 35
                                      WHEN 2 THEN 30
                                      WHEN 3 THEN 20
                                      WHEN 0 THEN 10
                                      ELSE 15
                                    END
                                    >
                                    CASE m.report_status
                                      WHEN 1 THEN 40
                                      WHEN 99 THEN 35
                                      WHEN 2 THEN 30
                                      WHEN 3 THEN 20
                                      WHEN 0 THEN 10
                                      ELSE 15
                                    END
                                    OR (
                                      CASE CAST(:report_status AS smallint)
                                        WHEN 1 THEN 40
                                        WHEN 99 THEN 35
                                        WHEN 2 THEN 30
                                        WHEN 3 THEN 20
                                        WHEN 0 THEN 10
                                        ELSE 15
                                      END
                                      =
                                      CASE m.report_status
                                        WHEN 1 THEN 40
                                        WHEN 99 THEN 35
                                        WHEN 2 THEN 30
                                        WHEN 3 THEN 20
                                        WHEN 0 THEN 10
                                        ELSE 15
                                      END
                                      AND CAST(:event_key AS char(64))
                                        >COALESCE(m.report_event_key,''::char(64))
                                    )
                                  )
                                )
                              )
                            )
                          )
                        RETURNING m.id
                        """
                    ),
                    {
                        "status": report.message_status,
                        "report_status": report.report_status,
                        "report_desc": report.report_desc,
                        "report_time": report.report_time,
                        "event_key": report.event_key,
                        "id": row["id"],
                        "created_at": row["created_at"],
                    },
                )
                changed = updated.scalar_one_or_none() is not None
                await connection.execute(
                    text(
                        """
                        UPDATE report_event_projection
                        SET projection_changed=:changed
                        WHERE event_key=CAST(:event_key AS char(64))
                        """
                    ),
                    {"event_key": report.event_key, "changed": changed},
                )
                if not changed:
                    return ReportApplyResult(batch_id, changed=False)
                # 晚到回执可能落在固定聚合窗口之外：标记消息归属日为脏，
                # 由 aggregate_stats 在近 5 天窗口之外补算（#342）。
                await connection.execute(
                    text(
                        """
                        INSERT INTO stat_dirty_date(stat_date)
                        VALUES (
                          CAST(
                            CAST(:created_at AS timestamptz)
                            AT TIME ZONE 'Asia/Shanghai' AS date
                          )
                        )
                        ON CONFLICT(stat_date) DO NOTHING
                        """
                    ),
                    {"created_at": row["created_at"]},
                )
                await self._refresh_batch(
                    connection,
                    batch_id,
                    batch_locked=True,
                    source_report_event_key=report.event_key,
                )
                await enqueue_message_report(
                    connection,
                    batch_id=batch_id,
                    message_id=int(row["id"]),
                    created_at=row["created_at"],
                    event_key=report.event_key,
                    message_status=report.message_status,
                    report_desc=report.report_desc,
                    report_time=report.report_time,
                )
                return ReportApplyResult(batch_id, changed=True)
        finally:
            await engine.dispose()

    async def failure_rate_candidate(self, batch_id: int) -> FailureRateAlert | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id batch_id,batch_no,status,delivered,failed,
                          COALESCE((SELECT value::int FROM sys_config
                            WHERE key='fail_rate_threshold'),20) threshold,
                          COALESCE((SELECT value::int FROM sys_config
                            WHERE key='fail_rate_min_total'),50) min_total
                        FROM sms_batch WHERE id=:batch_id
                        """
                    ),
                    {"batch_id": batch_id},
                )
                row = result.mappings().one_or_none()
                return evaluate_failure_rate(row) if row is not None else None
        finally:
            await engine.dispose()

    async def persist_unmatched(self, raw_id: int, report: ProtectedReport) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._persist_event(connection, raw_id, report)
                await connection.execute(
                    text(
                        """
                        INSERT INTO unmatched_report (
                          event_key,vendor_task_id,custom_id,
                          phone_enc,phone_hmac,phone_mask,
                          key_version,report_status,report_desc,report_time
                        ) SELECT
                          CAST(:event_key AS char(64)),
                          CAST(:vendor_task_id AS varchar(64)),
                          CAST(:custom_id AS varchar(64)),
                          :phone_enc,CAST(:phone_hmac AS char(64)),:phone_mask,
                          :key_version,CAST(:report_status AS smallint),
                          :report_desc,CAST(:report_time AS timestamptz)
                        ON CONFLICT(event_key) DO NOTHING
                        """
                    ),
                    {
                        "event_key": report.event_key,
                        "vendor_task_id": report.vendor_task_id,
                        "custom_id": report.custom_id,
                        "phone_enc": report.phone_enc,
                        "phone_hmac": report.phone_hmac,
                        "phone_mask": report.phone_mask,
                        "key_version": report.key_version,
                        "report_status": report.report_status,
                        "report_desc": report.report_desc,
                        "report_time": report.report_time,
                    },
                )
        finally:
            await engine.dispose()

    async def mark_processed(
        self,
        raw_id: int,
        *,
        lease: RawProcessingLease | None = None,
        system_audit_intent: bool = False,
    ) -> None:
        await self._mark_raw(
            raw_id,
            processed=True,
            error=None,
            parse_state=PARSE_PROCESSED,
            replay_eligibility=ELIGIBILITY_NEVER,
            lease=lease,
            system_audit_intent=system_audit_intent,
        )

    async def mark_error(
        self, raw_id: int, error: str, *, lease: RawProcessingLease | None = None
    ) -> None:
        columns = mark_error_column_values(error)
        await self._mark_raw(
            raw_id,
            processed=False,
            error=error,
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
        system_audit_intent: bool = False,
    ) -> None:
        token = self._lease_for(raw_id, lease)
        engine = self._engine()
        params: dict[str, Any] = {
            "id": raw_id,
            "processed": processed,
            "error": error,
            "parse_state": parse_state,
            "replay_eligibility": replay_eligibility,
            "lease_id": str(token.lease_id),
            "epoch": token.epoch,
        }
        if system_audit_intent:
            params["system_replay_audit_state"] = SYSTEM_REPLAY_AUDIT_PENDING
        try:
            await commit_fenced_raw_update(
                engine,
                fenced_terminal_sql(system_audit_intent=system_audit_intent),
                params,
                lease=token,
            )
        finally:
            await engine.dispose()
        self._leases.pop(raw_id, None)

    async def expire_unknown(self, timeout_hours: int) -> int:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                candidates = await connection.execute(
                    text(
                        """
                        SELECT DISTINCT m.batch_id
                        FROM sms_message m JOIN sms_chunk c ON c.id=m.chunk_id
                        WHERE m.chunk_id=c.id AND m.status='sent'
                          AND c.submitted_at < now() - make_interval(hours=>:hours)
                        ORDER BY m.batch_id
                        """
                    ),
                    {"hours": timeout_hours},
                )
                batch_ids = [int(value) for value in candidates.scalars()]
                refreshed = 0
                for batch_id in batch_ids:
                    await self._lock_batch(connection, batch_id)
                    updated = await connection.execute(
                        text(
                            """
                            WITH expired AS (
                              UPDATE sms_message m SET status='unknown'
                              FROM sms_chunk c
                              WHERE m.chunk_id=c.id AND m.batch_id=:batch_id
                                AND m.status='sent'
                                AND c.submitted_at < now()-make_interval(hours=>:hours)
                              RETURNING m.created_at
                            ), dirty AS (
                              INSERT INTO stat_dirty_date(stat_date)
                              SELECT DISTINCT CAST(
                                created_at AT TIME ZONE 'Asia/Shanghai' AS date
                              ) FROM expired
                              ON CONFLICT(stat_date) DO NOTHING
                            )
                            SELECT count(*) FROM expired
                            """
                        ),
                        {"batch_id": batch_id, "hours": timeout_hours},
                    )
                    if int(updated.scalar_one()) == 0:
                        continue
                    await self._refresh_batch(connection, batch_id, batch_locked=True)
                    refreshed += 1
                return refreshed
        finally:
            await engine.dispose()


def evaluate_failure_rate(row: Mapping[str, Any]) -> FailureRateAlert | None:
    """用整数交叉乘法判断终态批次失败率，避免浮点阈值漂移。"""

    delivered = int(row["delivered"])
    failed = int(row["failed"])
    threshold = int(row["threshold"])
    denominator = delivered + failed
    if (
        str(row["status"]) != "completed"
        or denominator < int(row["min_total"])
        or failed * 100 <= threshold * denominator
    ):
        return None
    return FailureRateAlert(
        batch_id=int(row["batch_id"]),
        batch_no=str(row["batch_no"]),
        delivered=delivered,
        failed=failed,
        threshold=threshold,
    )
