"""状态报告 raw、消息回写与 unmatched 的 PostgreSQL 仓储。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.report_timeout import SweepResult

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
    record_raw_heartbeat_lost,
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
from app.services.report_projection import (
    NO_REPORT_EVIDENCE,
    TRUSTED_REPORT_EVIDENCE,
    repair_message_projection_from_report,
)
from app.settings import Settings, get_settings
from app.vendor.routing import report_may_apply

LOGGER = logging.getLogger(__name__)


class SqlReportRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._leases: dict[int, RawProcessingLease] = {}
        self.on_claimed_batch: Callable[[Any, int], Awaitable[None]] | None = None

    def remember_lease(self, lease: RawProcessingLease) -> None:
        self._leases[lease.raw_id] = lease

    def _lease_for(self, raw_id: int, lease: RawProcessingLease | None) -> RawProcessingLease:
        return require_lease(lease or self._leases.get(raw_id), raw_id)

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def report_timeout_hours(self) -> int:
        return await self.require_report_timeout_hours()

    async def require_report_timeout_hours(self) -> int:
        """读取并校验无报告置 unknown 的时长；缺失或损坏立即失败。"""

        from app.services.report_timeout import parse_report_timeout_hours

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='report_timeout_hours'")
                )
                return parse_report_timeout_hours(result.scalar_one_or_none())
        finally:
            await engine.dispose()

    async def load_sweep_config(self) -> dict[str, str]:
        """读取超时扫描有界参数；缺键由调用方回落到注册默认值。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key, value FROM sys_config WHERE key IN (
                          'report_timeout_batch_limit',
                          'report_timeout_message_limit',
                          'report_timeout_round_seconds',
                          'report_timeout_statement_ms',
                          'report_timeout_lock_ms'
                        )
                        """
                    )
                )
                return {str(row["key"]): str(row["value"]) for row in result.mappings()}
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

    async def renew_processing_lease(self, lease: RawProcessingLease) -> datetime:
        engine = self._engine()
        try:
            return await renew_raw_lease(engine, lease)
        finally:
            await engine.dispose()

    async def record_heartbeat_failure(
        self,
        lease: RawProcessingLease,
        _error: Exception,
    ) -> None:
        """记录无 PII 的 heartbeat_lost；失败向外抛出让 Heartbeat 记日志。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await record_raw_heartbeat_lost(
                    connection, raw_id=lease.raw_id, lease_id=lease.lease_id
                )
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
        unknown_delta: int | None = None,
        status_delta: tuple[str, str] | None = None,
    ) -> None:
        if not batch_locked:
            await cls._lock_batch(connection, batch_id)
        used_delta = False
        if unknown_delta is not None or status_delta is not None:
            delivered_delta = failed_delta = active_delta = 0
            if status_delta is not None:
                # 旧状态在 chunk → batch → message 锁内读取；只有有效 UPDATE 才记差值。
                previous, current = status_delta
                if current not in {"delivered", "failed", "unknown", "other"}:
                    raise ValueError("report delta must end in a report status")
                delivered_delta = int(current == "delivered") - int(previous == "delivered")
                failed_delta = int(current == "failed") - int(previous == "failed")
                unknown_delta = int(current == "unknown") - int(previous == "unknown")
                active_delta = int(current in {"pending", "sent"}) - int(
                    previous in {"pending", "sent"}
                )
            elif unknown_delta is not None:
                if unknown_delta < 0:
                    raise ValueError("unknown_delta must be non-negative")
                active_delta = -unknown_delta  # timeout 已知只更新 sent → unknown。
            updated = await connection.execute(
                text(
                    """
                    UPDATE sms_batch b SET
                      delivered=b.delivered + :delivered_delta,
                      failed=b.failed + :failed_delta,
                      unknown_cnt=b.unknown_cnt + :delta,
                      active_message_count=b.active_message_count + :active_delta,
                      active_message_count_token=gen_random_uuid(),
                      status=CASE
                        WHEN b.status='completed_unknown' THEN 'completed_unknown'
                        WHEN b.active_message_count + :active_delta=0 THEN 'completed'
                        ELSE b.status
                      END,
                      updated_at=now()
                    WHERE b.id=:batch_id
                      AND b.active_message_count IS NOT NULL
                      AND b.active_message_count_token IS NOT NULL
                    RETURNING b.id
                    """
                ),
                {
                    "batch_id": batch_id,
                    "delta": unknown_delta,
                    "delivered_delta": delivered_delta,
                    "failed_delta": failed_delta,
                    "active_delta": active_delta,
                },
            )
            used_delta = updated.scalar_one_or_none() is not None
        if not used_delta:
            # 新批次和历史批次均惰性初始化。此时消息 UPDATE 已发生，按当前事实
            # 一次重算所有计数，不再叠加本次 delta；可信投影修复也沿用此路径。
            await connection.execute(
                text(
                    """
                    UPDATE sms_batch b SET
                      delivered=s.delivered, failed=s.failed, unknown_cnt=s.unknown_cnt,
                      active_message_count=s.active,
                      active_message_count_token=gen_random_uuid(),
                      status=CASE
                        WHEN b.status='completed_unknown' THEN 'completed_unknown'
                        WHEN s.message_count=0 THEN b.status
                        WHEN s.active=0 THEN 'completed'
                        ELSE b.status
                      END,
                      updated_at=now()
                    FROM (
                      SELECT count(*) message_count,
                        count(*) FILTER (WHERE status='delivered') delivered,
                        count(*) FILTER (WHERE status='failed') failed,
                        count(*) FILTER (WHERE status='unknown') unknown_cnt,
                        count(*) FILTER (WHERE status IN ('pending','sent')) active
                      FROM sms_message WHERE batch_id=:batch_id
                    ) s WHERE b.id=:batch_id
                    """
                ),
                {"batch_id": batch_id},
            )
        await enqueue_batch_finished(
            connection,
            batch_id,
            source_report_event_key=source_report_event_key,
        )
        status = (
            await connection.execute(
                text("SELECT status FROM sms_batch WHERE id=:batch_id"),
                {"batch_id": batch_id},
            )
        ).scalar_one()
        if str(status) in {"completed", "completed_unknown"}:
            from app.services.send_inflight import request_inflight_release_for_batch

            await request_inflight_release_for_batch(
                connection,
                batch_id=int(batch_id),
                reason=(
                    "batch-completed-unknown"
                    if str(status) == "completed_unknown"
                    else "batch-completed"
                ),
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
                        SELECT m.id,m.created_at,m.batch_id,
                               c.id chunk_id,
                               COALESCE(c.selected_vendor,'zhihui') selected_vendor,
                               (
                                 SELECT coalesce(array_agg(a.vendor_id), '{}')
                                 FROM sms_vendor_attempt a
                                 WHERE a.chunk_id=c.id
                                   AND a.outcome IN ('submitted','uncertain')
                               ) irreversible_vendors
                        FROM sms_chunk c
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
                # 与发送终结保持 chunk → batch → message 的锁顺序。
                # 锁后再读供应商归属，避免锁外快照跨过 failover 转换。
                authority = (
                    await connection.execute(
                        text(
                            """
                            SELECT COALESCE(c.selected_vendor,'zhihui') selected_vendor,
                              (SELECT coalesce(array_agg(a.vendor_id), '{}')
                               FROM sms_vendor_attempt a WHERE a.chunk_id=c.id
                               AND a.outcome IN ('submitted','uncertain')) irreversible_vendors
                            FROM sms_chunk c WHERE c.id=:id FOR UPDATE OF c
                            """
                        ),
                        {"id": row["chunk_id"]},
                    )
                ).mappings().one_or_none()
                if authority is None:
                    return None
                raw_vendors = authority.get("irreversible_vendors") or ()
                irreversible = frozenset(str(item) for item in raw_vendors)
                if not report_may_apply(
                    report_vendor_id=getattr(report, "vendor_id", "zhihui"),
                    selected_vendor=str(authority.get("selected_vendor") or "zhihui"),
                    irreversible_vendors=irreversible,
                ):
                    return None
                batch_id = int(row["batch_id"])
                await self._lock_batch(connection, batch_id)
                locked_message = await connection.execute(
                    text(
                        f"""
                        SELECT id,created_at,batch_id,status,
                          CASE WHEN {TRUSTED_REPORT_EVIDENCE} THEN
                            status IS DISTINCT FROM (
                              SELECT e.message_status FROM report_event e
                              WHERE e.event_key=m.report_event_key
                            )
                          ELSE false END prior_report_state_drift
                        FROM sms_message m
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
                    repaired = await self._repair_report_projection(
                        connection, batch_id, [(int(row["id"]), row["created_at"])]
                    )
                    return ReportApplyResult(batch_id, changed=bool(repaired))
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
                    # 可信旧事实与消息状态漂移时，不能拿损坏的状态计算增量。
                    # 仅实际生效的新事件回退到既有全量校正；未生效仍保持原语义。
                    status_delta=(
                        None if row["prior_report_state_drift"]
                        else (str(row["status"]), report.message_status)
                    ),
                )
                await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk c
                        SET late_evidence_at=COALESCE(c.late_evidence_at,now())
                        FROM sms_message m
                        WHERE m.id=:id AND m.created_at=:created_at
                          AND c.id=m.chunk_id
                          AND c.status='unknown_terminal'
                        """
                    ),
                    {"id": row["id"], "created_at": row["created_at"]},
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

    async def _repair_report_projection(
        self,
        connection: AsyncConnection,
        batch_id: int,
        identities: list[tuple[int, datetime]],
    ) -> bool:
        """在既有锁内修复投影，并沿用同一报告事件的回调及容量幂等身份。"""

        repaired = await repair_message_projection_from_report(
            connection, batch_id=batch_id, identities=identities
        )
        if repaired:
            await self._refresh_batch(
                connection,
                batch_id,
                batch_locked=True,
                source_report_event_key=str(repaired[-1]["report_event_key"]),
            )
        for row in repaired:
            event_key = str(row["report_event_key"])
            await enqueue_message_report(
                connection,
                batch_id=batch_id,
                message_id=int(row["id"]),
                created_at=row["created_at"],
                event_key=event_key,
                message_status=str(row["status"]),
                report_desc=str(row["report_desc"]),
                report_time=row["report_time"],
            )
        return bool(repaired)

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

    async def expire_due_reports(
        self,
        *,
        timeout_hours: int,
        batch_limit: int,
        message_limit_per_batch: int,
        max_round_seconds: float,
        statement_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> SweepResult:
        """有界短事务扫描到期 sent 消息；每批独立提交，锁冲突 SKIP LOCKED。"""

        from app.services.report_timeout import SweepResult, parse_report_timeout_hours

        hours = parse_report_timeout_hours(timeout_hours)
        engine = self._engine()
        candidates = 0
        batches_changed = 0
        messages_changed = 0
        skipped_locked = 0
        failed = 0
        more_remaining = False
        visited: set[int] = set()
        deadline = monotonic() + max_round_seconds
        try:
            async with asyncio.timeout(max_round_seconds):
                for _attempt in range(batch_limit):
                    remaining_s = deadline - monotonic()
                    if remaining_s <= 0:
                        more_remaining = True
                        break
                    remaining_ms = max(1, int(remaining_s * 1000))
                    try:
                        outcome = await self._expire_one_due_batch(
                            engine,
                            timeout_hours=hours,
                            message_limit=message_limit_per_batch,
                            exclude_ids=visited,
                            statement_timeout_ms=min(statement_timeout_ms, remaining_ms),
                            lock_timeout_ms=min(lock_timeout_ms, remaining_ms),
                        )
                    except asyncio.CancelledError:
                        raise
                    except TimeoutError:
                        more_remaining = True
                        break
                    except Exception:
                        failed += 1
                        more_remaining = True
                        LOGGER.warning("report timeout storage unavailable")
                        break
                    if outcome.kind == "empty":
                        if await self._due_batches_exist(
                            engine,
                            timeout_hours=hours,
                            exclude_ids=visited,
                        ):
                            skipped_locked += 1
                            more_remaining = True
                        break
                    visited.add(outcome.batch_id)
                    candidates += 1
                    if outcome.kind == "failed":
                        failed += 1
                        more_remaining = True
                        continue
                    if outcome.messages_changed:
                        batches_changed += 1
                        messages_changed += outcome.messages_changed
                    if outcome.more_in_batch:
                        more_remaining = True
                if len(visited) >= batch_limit:
                    more_remaining = True
        except TimeoutError:
            more_remaining = True
        finally:
            await engine.dispose()
        return SweepResult(
            candidates=candidates,
            batches_changed=batches_changed,
            messages_changed=messages_changed,
            skipped_locked=skipped_locked,
            failed=failed,
            more_remaining=more_remaining,
        )

    async def _due_batches_exist(
        self,
        engine: Any,
        *,
        timeout_hours: int,
        exclude_ids: set[int],
    ) -> bool:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM sms_batch b
                      WHERE EXISTS (
                        SELECT 1
                        FROM sms_message m
                        JOIN sms_chunk c ON c.id=m.chunk_id
                        WHERE m.batch_id=b.id
                          AND m.status='sent'
                          AND c.submitted_at < now() - make_interval(hours=>:hours)
                      )
                      AND NOT (b.id = ANY(CAST(:exclude_ids AS bigint[])))
                    )
                    """
                ),
                {"hours": timeout_hours, "exclude_ids": list(exclude_ids)},
            )
            return bool(result.scalar_one())

    async def _claim_due_timeout_batch(
        self, engine: Any, *, timeout_hours: int, exclude_ids: set[int],
        statement_timeout_ms: int, lock_timeout_ms: int,
    ) -> tuple[int, int] | None:
        """独立提交领取代际和调度位置；租约覆盖最大 120 秒轮次及清理余量。"""

        async with engine.begin() as connection:
            await _apply_local_timeouts(
                connection, statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
            )
            claimed = await connection.execute(
                text(
                    """
                    SELECT b.id
                    FROM sms_batch b
                    WHERE EXISTS (
                      SELECT 1
                      FROM sms_message m
                      JOIN sms_chunk c ON c.id=m.chunk_id
                      WHERE m.batch_id=b.id
                        AND m.status='sent'
                        AND c.submitted_at < now() - make_interval(hours=>:hours)
                    )
                    AND NOT (b.id = ANY(CAST(:exclude_ids AS bigint[])))
                    AND (b.report_timeout_next_attempt_at IS NULL
                      OR b.report_timeout_next_attempt_at <= now())
                    ORDER BY b.report_timeout_last_attempt_at NULLS FIRST, b.id
                    LIMIT 1
                    FOR UPDATE OF b SKIP LOCKED
                    """
                ),
                {"hours": timeout_hours, "exclude_ids": list(exclude_ids)},
            )
            raw_id = claimed.scalar_one_or_none()
            if raw_id is None:
                return None
            row = (await connection.execute(
                text("""
                    UPDATE sms_batch SET
                      report_timeout_generation=report_timeout_generation+1,
                      report_timeout_last_attempt_at=clock_timestamp(),
                      report_timeout_next_attempt_at=clock_timestamp()+interval '150 seconds'
                    WHERE id=:id RETURNING id,report_timeout_generation
                """), {"id": int(raw_id)},
            )).one()
            return int(row[0]), int(row[1])

    async def _record_timeout_failure(
        self, engine: Any, *, batch_id: int, generation: int,
        statement_timeout_ms: int, lock_timeout_ms: int,
    ) -> None:
        """业务回滚后独立保存退避；旧代际或已终结批次不得被迟到失败覆盖。"""

        async with engine.begin() as connection:
            await _apply_local_timeouts(
                connection, statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
            )
            await connection.execute(text("""
                UPDATE sms_batch b SET
                  report_timeout_generation=report_timeout_generation+1,
                  report_timeout_failures=LEAST(report_timeout_failures+1,16),
                  report_timeout_next_attempt_at=clock_timestamp()+make_interval(
                    secs=>LEAST(300, power(2,LEAST(report_timeout_failures+1,9)))
                  )
                WHERE b.id=:id AND b.report_timeout_generation=:generation
                  AND b.report_timeout_next_attempt_at IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM sms_message m WHERE m.batch_id=b.id AND m.status='sent'
                  )
            """), {"id": batch_id,"generation": generation})

    async def _expire_one_due_batch(
        self,
        engine: Any,
        *,
        timeout_hours: int,
        message_limit: int,
        exclude_ids: set[int],
        statement_timeout_ms: int,
        lock_timeout_ms: int,
    ) -> _BatchSweepOutcome:
        claim = await self._claim_due_timeout_batch(
            engine, timeout_hours=timeout_hours, exclude_ids=exclude_ids,
            statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms,
        )
        if claim is None:
            return _BatchSweepOutcome("empty", 0, 0, False)
        batch_id, generation = claim
        try:
            async with engine.begin() as connection:
                await _apply_local_timeouts(
                    connection, statement_timeout_ms=statement_timeout_ms,
                    lock_timeout_ms=lock_timeout_ms,
                )
                owned = (await connection.execute(text("""
                    SELECT id FROM sms_batch
                    WHERE id=:id AND report_timeout_generation=:generation
                    FOR UPDATE SKIP LOCKED
                """), {"id": batch_id,"generation": generation})).scalar_one_or_none()
                if owned is None:
                    return _BatchSweepOutcome("deferred", batch_id, 0, True)
                try:
                    if self.on_claimed_batch is not None:
                        await self.on_claimed_batch(connection, batch_id)
                    picked = await connection.execute(
                        text(
                            f"""
                            SELECT m.id, m.created_at
                            FROM sms_message m
                            JOIN sms_chunk c ON c.id=m.chunk_id
                            WHERE m.batch_id=:batch_id
                              AND m.status='sent'
                              AND ({NO_REPORT_EVIDENCE} OR {TRUSTED_REPORT_EVIDENCE})
                              AND c.submitted_at < now() - make_interval(hours=>:hours)
                            ORDER BY m.id, m.created_at
                            LIMIT :message_limit
                            FOR UPDATE OF m
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "hours": timeout_hours,
                            "message_limit": message_limit,
                        },
                    )
                    rows = list(picked.mappings())
                    await self._repair_report_projection(
                        connection,
                        batch_id,
                        [(int(row["id"]), row["created_at"]) for row in rows],
                    )
                    updated = await connection.execute(
                        text(
                            f"""
                            WITH expired AS (
                              UPDATE sms_message m SET status='unknown'
                              FROM sms_chunk c,
                              unnest(
                                CAST(:ids AS bigint[]),
                                CAST(:created_ats AS timestamptz[])
                              ) AS p(id, created_at)
                              WHERE m.id=p.id AND m.created_at=p.created_at
                                AND m.chunk_id=c.id
                                AND m.batch_id=:batch_id
                                AND m.status='sent'
                                AND {NO_REPORT_EVIDENCE}
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
                        {
                            "batch_id": batch_id,
                            "hours": timeout_hours,
                            "ids": [int(row["id"]) for row in rows],
                            "created_ats": [row["created_at"] for row in rows],
                        },
                    )
                    changed = int(updated.scalar_one())
                    remaining = await self._batch_has_due_sent(
                        connection, batch_id=batch_id, timeout_hours=timeout_hours
                    )
                    if changed:
                        await self._refresh_batch(
                            connection,
                            batch_id,
                            batch_locked=True,
                            unknown_delta=changed,
                        )
                    await connection.execute(text("""
                        UPDATE sms_batch SET report_timeout_failures=0,
                          report_timeout_next_attempt_at=NULL
                        WHERE id=:id AND report_timeout_generation=:generation
                    """), {"id": batch_id,"generation": generation})
                    return _BatchSweepOutcome("changed", batch_id, changed, remaining)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._record_timeout_failure(
                engine, batch_id=batch_id, generation=generation,
                statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms,
            )
            return _BatchSweepOutcome("failed", batch_id, 0, False)

    @staticmethod
    async def _batch_has_due_sent(
        connection: AsyncConnection,
        *,
        batch_id: int,
        timeout_hours: int,
    ) -> bool:
        result = await connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM sms_message m
                  JOIN sms_chunk c ON c.id=m.chunk_id
                  WHERE m.batch_id=:batch_id
                    AND m.status='sent'
                    AND c.submitted_at < now() - make_interval(hours=>:hours)
                )
                """
            ),
            {"batch_id": batch_id, "timeout_hours": timeout_hours, "hours": timeout_hours},
        )
        return bool(result.scalar_one())


@dataclass(frozen=True, slots=True)
class _BatchSweepOutcome:
    kind: str
    batch_id: int
    messages_changed: int
    more_in_batch: bool


async def _apply_local_timeouts(
    connection: AsyncConnection,
    *,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> None:
    await connection.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": str(statement_timeout_ms)},
    )
    await connection.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": str(lock_timeout_ms)},
    )


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
