"""生命周期清理 PostgreSQL 仓储，明确排除 audit_log。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.housekeeping import CleanupCounts, ExpiredImport, LifecyclePolicy
from app.settings import Settings, get_settings


class SqlHousekeepingRepository:
    """运行态只使用既有 DML 权限清理可再生或已过期数据。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def policy(self) -> LifecyclePolicy:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key,value FROM sys_config WHERE key IN (
                          'raw_log_retention_days','unmatched_retention_days',
                          'job_history_days','usage_ledger_retention_days'
                        )
                        """
                    )
                )
                values = {str(row["key"]): int(row["value"]) for row in result.mappings()}
                policy = LifecyclePolicy(
                    values.get("raw_log_retention_days", 90),
                    values.get("unmatched_retention_days", 90),
                    values.get("job_history_days", 30),
                    values.get("usage_ledger_retention_days", 90),
                )
                if (
                    min(
                        policy.raw_days,
                        policy.unmatched_days,
                        policy.job_days,
                        policy.usage_days,
                    )
                    < 1
                ):
                    raise ValueError("lifecycle retention values must be positive")
                return policy
        finally:
            await engine.dispose()

    async def expired_imports(self) -> tuple[ExpiredImport, ...]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,invalid_file,source_file FROM import_task
                        WHERE (
                            state='ready' AND expires_at<=now()
                          )
                          OR (
                            state='reserved' AND expires_at<=now()
                            AND reservation_expires_at<=now()
                          )
                          OR (
                            state='consumed' AND expires_at<=now()
                            AND payload_purged_at IS NULL
                          )
                        ORDER BY id
                        """
                    )
                )
                return tuple(
                    ExpiredImport(
                        int(row["id"]),
                        str(row["invalid_file"]) if row["invalid_file"] is not None else None,
                        str(row["source_file"]) if row["source_file"] is not None else None,
                    )
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def cleanup(
        self,
        policy: LifecyclePolicy,
        import_ids: tuple[int, ...],
    ) -> CleanupCounts:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH deleted_raw AS (
                          DELETE FROM raw_vendor_log
                          WHERE fetched_at < now()-make_interval(days=>:raw_days)
                          RETURNING 1
                        ), deleted_unmatched AS (
                          DELETE FROM unmatched_report
                          WHERE created_at < now()-make_interval(days=>:unmatched_days)
                          RETURNING 1
                        ), purged_consumed_import_phones AS (
                          DELETE FROM import_phone p
                          USING import_task t
                          WHERE p.import_task_id=t.id
                            AND t.id=ANY(CAST(:import_ids AS bigint[]))
                            AND t.expires_at<=now() AND t.state='consumed'
                          RETURNING 1
                        ), sanitized_consumed_imports AS (
                          UPDATE import_task SET
                            invalid_file=NULL,payload_purged_at=now()
                          WHERE id=ANY(CAST(:import_ids AS bigint[]))
                            AND expires_at<=now() AND state='consumed'
                            AND payload_purged_at IS NULL
                          RETURNING 1
                        ), deleted_imports AS (
                          DELETE FROM import_task
                          WHERE id=ANY(CAST(:import_ids AS bigint[]))
                            AND expires_at<=now()
                            AND (
                              state='ready'
                              OR (
                                state='reserved'
                                AND reservation_expires_at<=now()
                              )
                            )
                          RETURNING 1
                        ), deleted_idempotency AS (
                          DELETE FROM idempotency_record WHERE expires_at<=now()
                          RETURNING 1
                        ), deleted_jobs AS (
                          DELETE FROM job_run
                          WHERE started_at < now()-make_interval(days=>:job_days)
                          RETURNING 1
                        ), deleted_terminal_callbacks AS (
                          DELETE FROM callback_task
                          WHERE status IN ('done','dead')
                            AND created_at < now()-make_interval(days=>:raw_days)
                          RETURNING 1
                        ), deleted_callback_events AS (
                          DELETE FROM callback_report_event e
                          WHERE e.created_at < now()-make_interval(days=>:raw_days)
                            AND NOT EXISTS (
                              SELECT 1 FROM callback_task t
                              WHERE t.event_keys @>
                                ARRAY[e.event_key]::char(64)[]
                            )
                          RETURNING 1
                        ), deleted_usage AS (
                          DELETE FROM usage_reservation r
                          WHERE r.usage_date < (
                              (now() AT TIME ZONE 'Asia/Shanghai')::date
                              - CAST(:usage_days AS integer)
                            )
                            AND r.state IN ('released','committed')
                            AND NOT EXISTS (
                              SELECT 1 FROM usage_quota_entry q
                              WHERE q.reservation_id=r.id AND q.expires_at>now()
                            )
                            AND NOT EXISTS (
                              SELECT 1 FROM usage_frequency_entry f
                              WHERE f.reservation_id=r.id AND f.expires_at>now()
                            )
                          RETURNING 1
                        ), deleted_usage_projection AS (
                          DELETE FROM usage_projection
                          WHERE expires_at < now()-make_interval(days=>:usage_days)
                          RETURNING 1
                        ), deleted_usage_subject AS (
                          DELETE FROM usage_frequency_subject s
                          WHERE NOT EXISTS (
                            SELECT 1 FROM usage_frequency_entry f
                            WHERE f.subject_id=s.id
                          )
                          RETURNING 1
                        )
                        SELECT
                          (SELECT count(*) FROM deleted_raw) raw,
                          (SELECT count(*) FROM deleted_unmatched) unmatched,
                          (
                            (SELECT count(*) FROM deleted_imports)
                            +(SELECT count(*) FROM sanitized_consumed_imports)
                          ) imports,
                          (SELECT count(*) FROM deleted_idempotency) idempotency,
                          (SELECT count(*) FROM deleted_jobs) jobs,
                          (SELECT count(*) FROM deleted_usage) usage
                        """
                    ),
                    {
                        "raw_days": policy.raw_days,
                        "unmatched_days": policy.unmatched_days,
                        "job_days": policy.job_days,
                        "usage_days": policy.usage_days,
                        "import_ids": list(import_ids),
                    },
                )
                row = result.mappings().one()
                return CleanupCounts(
                    int(row["raw"]),
                    int(row["unmatched"]),
                    int(row["imports"]),
                    int(row["idempotency"]),
                    int(row["jobs"]),
                    int(row["usage"]),
                )
        finally:
            await engine.dispose()
