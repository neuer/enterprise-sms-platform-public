"""仪表盘数据库快照查询。"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import text

from app.core.runtime_resources import database_engine, redis_client
from app.services.dashboard import (
    AlertSummary,
    BalancePoint,
    Category,
    CategoryTotals,
    DashboardFacts,
    DashboardOperationsFacts,
    JobLatest,
)
from app.services.runtime_policy import RuntimePolicy
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)


class SqlDashboardRepository:
    """部门业务量与平台运行摘要均只返回聚合结果。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        channel_timeout_s: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client(self.settings.redis_control_url)
        self.channel_timeout_s = channel_timeout_s

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load(
        self,
        scope_dept: str | None,
        today: date,
        *,
        include_operations: bool,
    ) -> DashboardFacts:
        dim_type = "all" if scope_dept is None else "dept"
        dim_value = "" if scope_dept is None else scope_dept
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                stats_result = await connection.execute(
                    text(
                        """
                        SELECT category,total,total_segments,delivered,failed,unknown_cnt
                        FROM stat_daily
                        WHERE stat_date=:today AND dim_type=:dim_type
                          AND dim_value=:dim_value
                          AND category IN ('verify','notice','market')
                        ORDER BY category
                        """
                    ),
                    {"today": today, "dim_type": dim_type, "dim_value": dim_value},
                )
                categories = tuple(
                    CategoryTotals(
                        cast(Category, str(row["category"])),
                        int(row["total"]),
                        int(row["total_segments"]),
                        int(row["delivered"]),
                        int(row["failed"]),
                        int(row["unknown_cnt"]),
                    )
                    for row in stats_result.mappings()
                )
                if not include_operations:
                    pending_result = await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM approval ap
                            JOIN sms_batch b ON b.id=ap.batch_id
                            WHERE ap.status='pending'
                              AND b.dept=:scope_dept
                            """
                        ),
                        {"scope_dept": scope_dept},
                    )
                    return DashboardFacts(
                        categories=categories,
                        pending_approvals=int(pending_result.scalar_one()),
                    )
                current_result = await connection.execute(
                    text(
                        "SELECT balance FROM balance_snapshot "
                        "ORDER BY fetched_at DESC,id DESC LIMIT 1"
                    )
                )
                current_raw = current_result.scalar_one_or_none()
                current_balance = int(current_raw) if current_raw is not None else None
                balance_result = await connection.execute(
                    text(
                        """
                        SELECT stat_date,balance FROM (
                          SELECT DISTINCT ON (
                            (fetched_at AT TIME ZONE 'Asia/Shanghai')::date
                          )
                            (fetched_at AT TIME ZONE 'Asia/Shanghai')::date stat_date,
                            balance,fetched_at,id
                          FROM balance_snapshot
                          WHERE (fetched_at AT TIME ZONE 'Asia/Shanghai')::date
                            BETWEEN :start_date AND :today
                          ORDER BY (fetched_at AT TIME ZONE 'Asia/Shanghai')::date,
                            fetched_at DESC,id DESC
                        ) daily ORDER BY stat_date
                        """
                    ),
                    {"start_date": today - timedelta(days=13), "today": today},
                )
                balances = tuple(
                    BalancePoint(row["stat_date"], int(row["balance"]))
                    for row in balance_result.mappings()
                )
                pending_result = await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM approval ap
                        JOIN sms_batch b ON b.id=ap.batch_id
                        WHERE ap.status='pending'
                          AND (CAST(:scope_dept AS varchar(128)) IS NULL
                               OR b.dept=:scope_dept)
                        """
                    ),
                    {"scope_dept": scope_dept},
                )
                pending = int(pending_result.scalar_one())
                alerts_result = await connection.execute(
                    text(
                        """
                        SELECT level,title,created_at FROM alert_log
                        WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date=:today
                        ORDER BY created_at DESC,id DESC LIMIT 5
                        """
                    ),
                    {"today": today},
                )
                alerts = tuple(
                    AlertSummary(
                        str(row["level"]),
                        str(row["title"]),
                        row["created_at"],
                    )
                    for row in alerts_result.mappings()
                )
                counts_result = await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM sms_chunk c
                           JOIN sms_batch b ON b.id=c.batch_id
                           WHERE c.status='uncertain'
                             AND (CAST(:scope_dept AS varchar(128)) IS NULL
                                  OR b.dept=:scope_dept)) uncertain,
                          (SELECT count(*) FROM unmatched_report) unmatched,
                          (SELECT count(*) FROM callback_task cb
                           JOIN app ON app.id=cb.app_id
                           WHERE cb.status='dead'
                             AND (CAST(:scope_dept AS varchar(128)) IS NULL
                                  OR app.dept=:scope_dept)) callback_dead
                        """
                    ),
                    {"scope_dept": scope_dept},
                )
                counts = counts_result.mappings().one()
                jobs_result = await connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (job_name)
                          job_name,started_at last_run_at,status last_status
                        FROM job_run ORDER BY job_name,started_at DESC,id DESC
                        """
                    )
                )
                jobs = tuple(
                    JobLatest(
                        str(row["job_name"]),
                        row["last_run_at"],
                        str(row["last_status"]),
                    )
                    for row in jobs_result.mappings()
                )
                config_result = await connection.execute(
                    text(
                        """
                        SELECT key,value FROM sys_config
                        WHERE key IN (
                          'vendor_qps','reserved_realtime_qps',
                          'balance_alert_threshold','test_send_max'
                        )
                        """
                    )
                )
                policy = RuntimePolicy.from_mapping(
                    {str(row["key"]): str(row["value"]) for row in config_result.mappings()}
                )
                realtime_queue: int | None = None
                bulk_queue: int | None = None
                qps_used: int | None = None
                channel_stale = True
                try:
                    pipeline = self.redis.pipeline(transaction=False)
                    pipeline.llen("realtime")
                    pipeline.llen("bulk")
                    pipeline.hget("ratelimit:vendor", "tokens")
                    async with asyncio.timeout(self.channel_timeout_s):
                        realtime_raw, bulk_raw, tokens_raw = await pipeline.execute()
                    realtime_queue = int(realtime_raw)
                    bulk_queue = int(bulk_raw)
                    remaining = (
                        policy.vendor_qps if tokens_raw is None else int(float(str(tokens_raw)))
                    )
                    qps_used = max(0, min(policy.vendor_qps, policy.vendor_qps - remaining))
                    channel_stale = False
                except Exception as error:
                    LOGGER.warning(
                        "dashboard channel monitor unavailable",
                        extra={"error_type": type(error).__name__},
                    )
                return DashboardFacts(
                    categories=categories,
                    pending_approvals=pending,
                    test_send_max=policy.test_send_max,
                    operations=DashboardOperationsFacts(
                        current_balance,
                        balances,
                        alerts,
                        int(counts["uncertain"]),
                        int(counts["unmatched"]),
                        int(counts["callback_dead"]),
                        jobs,
                        realtime_queue,
                        bulk_queue,
                        qps_used,
                        policy.vendor_qps,
                        policy.reserved_realtime_qps,
                        channel_stale,
                        policy.balance_alert_threshold,
                    ),
                )
        finally:
            await engine.dispose()
