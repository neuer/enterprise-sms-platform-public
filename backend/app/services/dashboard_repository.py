"""仪表盘数据库快照查询。"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import text

from app.core.jobtrack import JobSpec
from app.core.runtime_resources import database_engine, redis_client
from app.services.current_alerts import DatabaseCurrentFacts
from app.services.dashboard import (
    AlertSummary,
    BalancePoint,
    BalanceSnapshot,
    Category,
    CategoryTotals,
    ChannelMonitorDegradedReason,
    DashboardFacts,
    DashboardOperationsFacts,
    JobLatest,
    TrendDayTotals,
)
from app.services.runtime_policy import RuntimePolicy
from app.services.stats import SHANGHAI
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)


def _remaining_tokens(value: object, capacity: int) -> int:
    """校验令牌桶快照；空闲时不存在的桶按满容量解释。"""

    if value is None:
        # TokenBucket 在取令牌时才创建 hash，并在约 3 秒无活动后过期。
        # 因此 key/field 缺失表示没有活跃租约；下一次 acquire 会按 Lua
        # 实现从满桶开始，仪表盘可确定当前已用令牌为 0。
        return capacity
    try:
        remaining_float = float(str(value))
    except (TypeError, ValueError):
        raise ValueError("token snapshot is invalid") from None
    if not math.isfinite(remaining_float) or not remaining_float.is_integer():
        raise ValueError("token snapshot is invalid")
    remaining = int(remaining_float)
    if not 0 <= remaining <= capacity:
        raise ValueError("token snapshot is out of range")
    return remaining


class SqlDashboardRepository:
    """部门业务量与平台运行摘要均只返回聚合结果。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        channel_timeout_s: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis: Any = None
        self.channel_timeout_s = channel_timeout_s

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load_balance(self) -> BalanceSnapshot:
        """只读取最近一次余额事实；无快照保持未知，不加载仪表盘聚合。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT balance,fetched_at FROM balance_snapshot "
                        "ORDER BY fetched_at DESC,id DESC LIMIT 1"
                    )
                )
                row = result.mappings().first()
                return (
                    BalanceSnapshot(int(row["balance"]), row["fetched_at"])
                    if row is not None
                    else BalanceSnapshot(None, None)
                )
        finally:
            await engine.dispose()

    async def load(
        self,
        scope_dept: str | None,
        today: date,
        *,
        include_operations: bool,
        current_facts: DatabaseCurrentFacts | None = None,
        include_legacy_alerts: bool = True,
        job_specs: tuple[JobSpec, ...] = (),
    ) -> DashboardFacts:
        if current_facts is not None and (scope_dept is not None or not include_operations):
            raise ValueError("global current facts require an unscoped operational dashboard")
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
                trend_result = await connection.execute(
                    text(
                        """
                        SELECT stat_date,category,total
                        FROM stat_daily
                        WHERE stat_date BETWEEN :start_date AND :today
                          AND dim_type=:dim_type
                          AND dim_value=:dim_value
                          AND category IN ('verify','notice','market')
                        ORDER BY stat_date,category
                        """
                    ),
                    {
                        "start_date": today - timedelta(days=6),
                        "today": today,
                        "dim_type": dim_type,
                        "dim_value": dim_value,
                    },
                )
                trend = tuple(
                    TrendDayTotals(
                        row["stat_date"],
                        cast(Category, str(row["category"])),
                        int(row["total"]),
                    )
                    for row in trend_result.mappings()
                )
                if not include_operations:
                    pending_result = await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM approval ap
                            JOIN sms_batch b ON b.id=ap.batch_id
                            WHERE ap.status='pending'
                              AND (
                                CAST(:scope_dept AS text) IS NULL
                                OR b.dept=CAST(:scope_dept AS text)
                              )
                            """
                        ),
                        {"scope_dept": scope_dept},
                    )
                    return DashboardFacts(
                        categories=categories,
                        pending_approvals=int(pending_result.scalar_one()),
                        trend=trend,
                    )
                if current_facts is None:
                    current_result = await connection.execute(
                        text(
                            "SELECT balance FROM balance_snapshot "
                            "ORDER BY fetched_at DESC,id DESC LIMIT 1"
                        )
                    )
                    current_raw = current_result.scalar_one_or_none()
                    current_balance = int(current_raw) if current_raw is not None else None
                else:
                    current_balance = current_facts.balance
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
                          WHERE fetched_at>=:start_at AND fetched_at<:end_at
                          ORDER BY (fetched_at AT TIME ZONE 'Asia/Shanghai')::date,
                            fetched_at DESC,id DESC
                        ) daily ORDER BY stat_date
                        """
                    ),
                    {
                        "start_at": datetime.combine(
                            today - timedelta(days=13), time.min, tzinfo=SHANGHAI
                        ).astimezone(UTC),
                        "end_at": datetime.combine(
                            today + timedelta(days=1), time.min, tzinfo=SHANGHAI
                        ).astimezone(UTC),
                    },
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
                alerts: tuple[AlertSummary, ...] = ()
                if include_legacy_alerts:
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
                if current_facts is None:
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
                    queue_result = await connection.execute(
                        text(
                            """
                            SELECT lanes.queue,count(event.id) count
                            FROM (VALUES ('realtime'),('bulk')) AS lanes(queue)
                            LEFT JOIN outbox_event event
                              ON event.queue=lanes.queue
                             AND event.state IN ('pending','leased','published','processing')
                            GROUP BY lanes.queue
                            ORDER BY lanes.queue
                            """
                        )
                    )
                    queue_depths = {
                        str(row["queue"]): max(0, int(row["count"]))
                        for row in queue_result.mappings()
                    }
                    jobs_result = await connection.execute(
                        text(
                            """
                            SELECT s.job_name,r.started_at last_run_at,r.status last_status
                            FROM unnest(CAST(:job_names AS text[])) AS s(job_name)
                            JOIN LATERAL (
                              SELECT started_at,status FROM job_run
                              WHERE job_name=s.job_name
                              ORDER BY started_at DESC,id DESC LIMIT 1
                            ) r ON true
                            """
                        ),
                        {"job_names": [spec.job_name for spec in job_specs]},
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
                else:
                    unmatched_result = await connection.execute(
                        text("SELECT count(*) FROM unmatched_report")
                    )
                    counts = {
                        "uncertain": current_facts.uncertain,
                        "unmatched": int(unmatched_result.scalar_one()),
                        "callback_dead": current_facts.callback_dead,
                    }
                    queue_depths = {
                        "realtime": current_facts.realtime_queue,
                        "bulk": current_facts.bulk_queue,
                    }
                    jobs = tuple(
                        JobLatest(fact.job_name, fact.latest.started_at, fact.latest.status)
                        for fact in current_facts.jobs
                        if fact.latest is not None
                    )
                    policy = current_facts.policy
                qps_used: int | None = None
                channel_stale = True
                degraded_reason: ChannelMonitorDegradedReason | None = "redis_unavailable"
                try:
                    async with asyncio.timeout(self.channel_timeout_s):
                        if self.redis is None:
                            self.redis = redis_client(self.settings.redis_control_url)
                        tokens_raw = await self.redis.hget("ratelimit:vendor", "tokens")
                    remaining = _remaining_tokens(tokens_raw, policy.vendor_qps)
                    qps_used = max(0, min(policy.vendor_qps, policy.vendor_qps - remaining))
                    channel_stale = False
                    degraded_reason = None
                except ValueError:
                    degraded_reason = "snapshot_incomplete"
                except Exception as error:
                    LOGGER.warning(
                        "dashboard channel monitor unavailable",
                        extra={
                            "error_type": type(error).__name__,
                            "reason": degraded_reason,
                        },
                    )
                return DashboardFacts(
                    categories=categories,
                    pending_approvals=pending,
                    test_send_max=policy.test_send_max,
                    trend=trend,
                    operations=DashboardOperationsFacts(
                        current_balance,
                        balances,
                        alerts,
                        int(counts["uncertain"]),
                        int(counts["unmatched"]),
                        int(counts["callback_dead"]),
                        jobs,
                        queue_depths.get("realtime", 0),
                        queue_depths.get("bulk", 0),
                        qps_used,
                        policy.vendor_qps,
                        policy.reserved_realtime_qps,
                        channel_stale,
                        degraded_reason,
                        policy.balance_alert_threshold,
                    ),
                )
        finally:
            await engine.dispose()
