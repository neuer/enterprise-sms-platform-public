"""当前告警所需的 PostgreSQL 与 control Redis 权威事实读取。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import text

from app.core.jobtrack import JobRunSnapshot, JobSpec
from app.core.runtime_resources import database_engine, redis_client
from app.services.current_alerts import (
    ControlCurrentFacts,
    CurrentJobFact,
    DatabaseCurrentFacts,
    RawSpillAlertFact,
    UsageDriftFact,
)
from app.services.runtime_policy import RuntimePolicy
from app.settings import Settings, get_settings


class SqlCurrentAlertRepository:
    """只读取无 PII 聚合与运行元数据；不读取 raw 密文、手机号或短信内容。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        database_timeout_s: float = 3.0,
        control_timeout_s: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client(self.settings.redis_control_url)
        self.database_timeout_s = database_timeout_s
        self.control_timeout_s = control_timeout_s

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load_database(
        self,
        specs: tuple[JobSpec, ...],
    ) -> DatabaseCurrentFacts:
        engine = self._engine()
        try:
            async with asyncio.timeout(self.database_timeout_s), engine.connect() as connection:
                config_result = await connection.execute(text("SELECT key,value FROM sys_config"))
                policy = RuntimePolicy.from_mapping(
                    {str(row["key"]): str(row["value"]) for row in config_result.mappings()}
                )

                job_result = await connection.execute(
                    text(
                        """
                        WITH specs AS (
                          SELECT unnest(CAST(:job_names AS text[])) job_name
                        )
                        SELECT s.job_name,r.started_at,r.finished_at,r.status,
                          (
                            SELECT max(success.started_at) FROM job_run success
                            WHERE success.job_name=s.job_name AND success.status='success'
                          ) latest_success_at
                        FROM specs s
                        LEFT JOIN LATERAL (
                          SELECT started_at,finished_at,status
                          FROM job_run recent WHERE recent.job_name=s.job_name
                          ORDER BY started_at DESC,id DESC LIMIT 3
                        ) r ON true
                        ORDER BY s.job_name,r.started_at DESC NULLS LAST
                        """
                    ),
                    {"job_names": [spec.job_name for spec in specs]},
                )
                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                latest_success: dict[str, datetime | None] = {}
                for row in job_result.mappings():
                    job_name = str(row["job_name"])
                    latest_success[job_name] = cast(
                        datetime | None,
                        row["latest_success_at"],
                    )
                    if row["started_at"] is not None:
                        grouped[job_name].append(dict(row))
                jobs: list[CurrentJobFact] = []
                for spec in specs:
                    rows = grouped.get(spec.job_name, [])
                    latest = rows[0] if rows else None
                    jobs.append(
                        CurrentJobFact(
                            spec.job_name,
                            (
                                JobRunSnapshot(
                                    spec.job_name,
                                    cast(datetime, latest["started_at"]),
                                    cast(datetime | None, latest["finished_at"]),
                                    str(latest["status"]),
                                )
                                if latest is not None
                                else None
                            ),
                            tuple(str(row["status"]) for row in rows),
                            latest_success.get(spec.job_name),
                        )
                    )

                usage_result = await connection.execute(
                    text(
                        "SELECT kind,mismatched_dimensions,absolute_delta,checked_at "
                        "FROM usage_projection_drift ORDER BY kind"
                    )
                )
                usage = tuple(
                    UsageDriftFact(
                        str(row["kind"]),
                        int(row["mismatched_dimensions"]),
                        int(row["absolute_delta"]),
                        cast(datetime, row["checked_at"]),
                    )
                    for row in usage_result.mappings()
                )

                operation_result = await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT balance FROM balance_snapshot
                           ORDER BY fetched_at DESC,id DESC LIMIT 1) balance,
                          (SELECT fetched_at FROM balance_snapshot
                           ORDER BY fetched_at DESC,id DESC LIMIT 1) balance_checked_at,
                          (SELECT count(*) FROM sms_chunk
                           WHERE status='uncertain' AND uncertain_since <=
                             now()-make_interval(hours=>:uncertain_hours)) uncertain_overdue,
                          (SELECT min(uncertain_since) FROM sms_chunk
                           WHERE status='uncertain' AND uncertain_since <=
                             now()-make_interval(hours=>:uncertain_hours)) uncertain_since,
                          (SELECT count(*) FROM callback_task WHERE status='dead') callback_dead,
                          (SELECT min(created_at) FROM callback_task
                           WHERE status='dead') callback_dead_since,
                          (SELECT count(*) FROM outbox_event WHERE state='dead') outbox_dead,
                          (SELECT min(created_at) FROM outbox_event
                           WHERE state='dead') outbox_dead_since,
                          (SELECT count(*) FROM outbox_event
                           WHERE state IN ('pending','leased','published','processing'))
                             outbox_active,
                          (SELECT min(created_at) FROM outbox_event
                           WHERE state IN ('pending','leased','published','processing'))
                             outbox_oldest_active_at,
                          (SELECT count(*) FROM raw_vendor_log
                           WHERE processed=false
                             AND capture_state='complete_too_large'
                             AND replay_eligibility='manual') raw_manual,
                          (SELECT min(fetched_at) FROM raw_vendor_log
                           WHERE processed=false
                             AND capture_state='complete_too_large'
                             AND replay_eligibility='manual') raw_manual_since
                        """
                    ),
                    {"uncertain_hours": policy.uncertain_alert_hours},
                )
                operation = operation_result.mappings().one()

                spill_result = await connection.execute(
                    text(
                        """
                        SELECT alert_type,detail->>'source' source,max(created_at) created_at
                        FROM alert_log
                        WHERE alert_type IN (
                          'vendor_raw_spill_failed','vendor_raw_spill_quota_exceeded'
                        ) AND detail->>'source' IN ('report','reply')
                        GROUP BY alert_type,detail->>'source'
                        ORDER BY alert_type,detail->>'source'
                        """
                    )
                )
                spill_alerts = tuple(
                    RawSpillAlertFact(
                        str(row["alert_type"]),
                        cast(Literal["report", "reply"], str(row["source"])),
                        cast(datetime, row["created_at"]),
                    )
                    for row in spill_result.mappings()
                )
                return DatabaseCurrentFacts(
                    policy,
                    tuple(jobs),
                    usage,
                    int(operation["balance"])
                    if operation["balance"] is not None
                    else None,
                    cast(datetime | None, operation["balance_checked_at"]),
                    int(operation["uncertain_overdue"]),
                    cast(datetime | None, operation["uncertain_since"]),
                    int(operation["callback_dead"]),
                    cast(datetime | None, operation["callback_dead_since"]),
                    int(operation["outbox_dead"]),
                    cast(datetime | None, operation["outbox_dead_since"]),
                    int(operation["outbox_active"]),
                    cast(datetime | None, operation["outbox_oldest_active_at"]),
                    int(operation["raw_manual"]),
                    cast(datetime | None, operation["raw_manual_since"]),
                    spill_alerts,
                )
        finally:
            await engine.dispose()

    async def load_control(self) -> ControlCurrentFacts:
        async with asyncio.timeout(self.control_timeout_s):
            values = await self.redis.mget(
                "queue:paused:realtime",
                "queue:paused:bulk",
                "alert:vendor:consecutive_failures",
            )
        try:
            failures = int(values[2]) if values[2] is not None else 0
        except (TypeError, ValueError):
            raise ValueError("vendor failure counter is invalid") from None
        if failures < 0:
            raise ValueError("vendor failure counter is invalid")
        return ControlCurrentFacts(
            str(values[0]) if values[0] is not None else None,
            str(values[1]) if values[1] is not None else None,
            failures,
        )
