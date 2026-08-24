"""Prometheus 指标的 PostgreSQL 只读聚合仓储。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.metrics import MetricsFacts
from app.settings import Settings, get_settings


class SqlMetricsRepository:
    """只投影平台聚合值，禁止读取手机号、正文或厂商错误原文。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(
            self.settings.database_url_for("metrics"),
            component="metrics",
        )

    async def load(self) -> MetricsFacts:
        """在同一只读连接中收集一次 scrape 的数据库事实。"""

        engine = self._engine()
        async with engine.connect() as connection:
                rates_result = await connection.execute(
                    text(
                        """
                        SELECT b.category,
                          COALESCE(sum(c.phone_count),0)::double precision / 300.0 rate
                        FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.submitted_at>=now()-interval '5 minutes'
                        GROUP BY b.category
                        ORDER BY b.category
                        """
                    )
                )
                send_rates = tuple(
                    (str(row["category"]), max(0.0, float(row["rate"])))
                    for row in rates_result.mappings()
                )

                errors_result = await connection.execute(
                    text(
                        """
                        SELECT vendor_code::text code,count(vendor_code) count
                        FROM sms_chunk
                        WHERE vendor_code IS NOT NULL
                          AND status IN ('failed','retrying')
                        GROUP BY vendor_code
                        ORDER BY vendor_code
                        """
                    )
                )
                vendor_errors = tuple(
                    (str(row["code"]), max(0, int(row["count"])))
                    for row in errors_result.mappings()
                )

                counts_result = await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(status) FROM sms_chunk
                           WHERE status='uncertain') uncertain,
                          (SELECT count(status) FROM callback_task
                           WHERE status='retrying') callback_retrying,
                          (SELECT count(status) FROM callback_task
                           WHERE status='dead') callback_dead,
                          (SELECT count(lease_id) FROM callback_task
                           WHERE lease_id IS NOT NULL
                             AND lease_expires_at<=now()) callback_stalled,
                          (SELECT count(lease_id) FROM export_task
                           WHERE lease_id IS NOT NULL
                             AND lease_expires_at<=now()) export_stalled
                        """
                    )
                )
                counts = counts_result.mappings().one()

                queue_result = await connection.execute(
                    text(
                        """
                        SELECT queue,count(*) count
                        FROM outbox_event
                        WHERE queue IN ('realtime','bulk')
                          AND state IN ('pending','leased','published','processing')
                        GROUP BY queue
                        ORDER BY queue
                        """
                    )
                )
                queue_depths = tuple(
                    (str(row["queue"]), max(0, int(row["count"])))
                    for row in queue_result.mappings()
                )

                lease_events_result = await connection.execute(
                    text(
                        """
                        SELECT task_kind,event_type,count(event_type) count
                        FROM worker_lease_event
                        GROUP BY task_kind,event_type
                        ORDER BY task_kind,event_type
                        """
                    )
                )
                lease_events = tuple(
                    (
                        str(row["task_kind"]),
                        str(row["event_type"]),
                        max(0, int(row["count"])),
                    )
                    for row in lease_events_result.mappings()
                )

                frequency_result = await connection.execute(
                    text(
                        """
                        SELECT category,COALESCE(sum(removed_freq),0) count
                        FROM sms_batch
                        WHERE created_at >= (
                          date_trunc('day',now() AT TIME ZONE 'Asia/Shanghai')
                          AT TIME ZONE 'Asia/Shanghai'
                        )
                        GROUP BY category
                        ORDER BY category
                        """
                    )
                )
                frequency_filtered = tuple(
                    (str(row["category"]), max(0, int(row["count"])))
                    for row in frequency_result.mappings()
                )

                poll_result = await connection.execute(
                    text(
                        """
                        SELECT CASE job_name
                                 WHEN 'poll_report' THEN 'report'
                                 WHEN 'poll_reply' THEN 'reply'
                               END source,
                               EXTRACT(epoch FROM now()-max(finished_at)) lag_seconds
                        FROM job_run
                        WHERE job_name IN ('poll_report','poll_reply')
                          AND status='success' AND finished_at IS NOT NULL
                        GROUP BY job_name
                        ORDER BY source
                        """
                    )
                )
                poll_lags = tuple(
                    (str(row["source"]), max(0.0, float(row["lag_seconds"])))
                    for row in poll_result.mappings()
                )

                drift_result = await connection.execute(
                    text(
                        """
                        SELECT kind,mismatched_dimensions,absolute_delta
                        FROM usage_projection_drift ORDER BY kind
                        """
                    )
                )
                drift_rows = list(drift_result.mappings())

                eligibility_result = await connection.execute(
                    text(
                        """
                        SELECT replay_eligibility,count(replay_eligibility) count
                        FROM raw_vendor_log
                        GROUP BY replay_eligibility
                        ORDER BY replay_eligibility
                        """
                    )
                )
                raw_replay_eligibility = tuple(
                    (str(row["replay_eligibility"]), max(0, int(row["count"])))
                    for row in eligibility_result.mappings()
                )
                pending_audit = await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM raw_vendor_log
                        WHERE system_replay_audit_state='pending'
                        """
                    )
                )

                return MetricsFacts(
                    send_rates=send_rates,
                    vendor_errors=vendor_errors,
                    uncertain=max(0, int(counts["uncertain"])),
                    callback_failures=(
                        ("retrying", max(0, int(counts["callback_retrying"]))),
                        ("dead", max(0, int(counts["callback_dead"]))),
                    ),
                    frequency_filtered=frequency_filtered,
                    poll_lags=poll_lags,
                    usage_projection_mismatches=tuple(
                        (
                            str(row["kind"]),
                            max(0, int(row["mismatched_dimensions"])),
                        )
                        for row in drift_rows
                    ),
                    usage_projection_absolute_delta=tuple(
                        (
                            str(row["kind"]),
                            max(0, int(row["absolute_delta"])),
                        )
                        for row in drift_rows
                    ),
                    worker_stalled_leases=(
                        ("callback", max(0, int(counts.get("callback_stalled", 0)))),
                        ("export", max(0, int(counts.get("export_stalled", 0)))),
                    ),
                    worker_lease_events=lease_events,
                    queue_depths=queue_depths,
                    raw_replay_eligibility=raw_replay_eligibility,
                    system_replay_audit_pending=max(0, int(pending_audit or 0)),
                )
