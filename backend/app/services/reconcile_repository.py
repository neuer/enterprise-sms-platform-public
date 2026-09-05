"""停滞批次/分片扫描；submitted、uncertain 与 unknown_terminal 永不进入结果集。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.reconcile import RecoveryWork
from app.settings import Settings, get_settings
from app.vendor.zhihui import VENDOR_TIMEOUT_S

# 必须严格大于供应商绝对总超时 + 持久化预算 + 调度抖动；禁止只靠放大阈值掩盖竞态。
STALE_INVOKING_CUTOFF_S = 15 * 60
MAX_VENDOR_PERSIST_BUDGET_S = 30
SCHEDULER_JITTER_S = 60


def stale_invoking_cutoff_is_safe(vendor_timeout_s: float = VENDOR_TIMEOUT_S) -> bool:
    """契约：恢复器不得在活跃 HTTP owner 仍可能返回前接管 invoking。"""

    return (
        vendor_timeout_s + MAX_VENDOR_PERSIST_BUDGET_S + SCHEDULER_JITTER_S
    ) < STALE_INVOKING_CUTOFF_S


class SqlRecoveryRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def stalled(self) -> list[RecoveryWork]:
        engine: Any = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        WITH repaired AS (
                          UPDATE sms_vendor_attempt a
                          SET outcome='submitted', updated_at=now()
                          FROM sms_chunk c
                          WHERE a.chunk_id=c.id
                            AND a.outcome='invoking'
                            AND c.status='submitted'
                            AND c.route_generation=a.generation
                            AND c.vendor_task_id IS NOT NULL
                            AND c.submitted_at IS NOT NULL
                            AND EXISTS (
                              SELECT 1 FROM sms_message m
                              WHERE m.chunk_id=c.id AND m.status='sent'
                            )
                          RETURNING a.id
                        ), inconsistent AS (
                          UPDATE sms_vendor_attempt a
                          SET outcome='inconsistent', updated_at=now()
                          FROM sms_chunk c
                          WHERE a.chunk_id=c.id
                            AND a.outcome='invoking'
                            AND c.status IN (
                              'submitted','failed','uncertain','unknown_terminal'
                            )
                            AND a.id NOT IN (SELECT id FROM repaired)
                          RETURNING a.chunk_id
                        ), stale_invoking AS (
                          UPDATE sms_vendor_attempt a
                          SET outcome='uncertain', updated_at=now()
                          FROM sms_chunk c
                          WHERE a.chunk_id=c.id
                            AND a.outcome='invoking'
                            AND c.status='submitting'
                            AND c.route_generation=a.generation
                            AND a.invoke_started_at < now() - interval '15 minutes'
                          RETURNING a.chunk_id
                        ), stale_chunks AS (
                          UPDATE sms_chunk c SET
                            status='uncertain',
                            uncertain_since=COALESCE(c.submitting_since,now()),
                            submitting_since=NULL
                          FROM sms_batch b
                          WHERE b.id=c.batch_id
                            AND b.status IN ('queued','sending')
                            AND c.status='submitting'
                            AND (
                              c.submitting_since<now()-interval '5 minutes'
                              OR c.id IN (SELECT chunk_id FROM stale_invoking)
                            )
                          RETURNING c.id
                        ), settled AS (
                          UPDATE vendor_test_send_attempt attempt SET
                            status='uncertain',settled_at=now()
                          FROM stale_chunks stale
                          WHERE attempt.chunk_id=stale.id
                            AND attempt.status='reserved'
                          RETURNING attempt.usage_date,attempt.segments
                        ), totals AS (
                          SELECT usage_date,sum(segments)::integer AS segments
                          FROM settled GROUP BY usage_date
                        )
                        UPDATE vendor_test_daily_usage usage SET
                          in_flight_segments=usage.in_flight_segments-totals.segments,
                          uncertain_segments=usage.uncertain_segments+totals.segments,
                          updated_at=now()
                        FROM totals WHERE usage.usage_date=totals.usage_date
                        """
                    )
                )
                batches = await connection.execute(
                    text(
                        """
                        SELECT trim(batch_no) batch_no,category FROM sms_batch
                        WHERE status='queued' AND updated_at<now()-interval '5 minutes'
                        ORDER BY updated_at LIMIT 100
                        """
                    )
                )
                chunks = await connection.execute(
                    text(
                        """
                        SELECT trim(b.batch_no) batch_no,b.category,c.id chunk_id
                        FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id
                        WHERE b.status='sending' AND (
                          c.status='pending' OR (
                            c.status='retrying' AND c.retry_not_before<=now()
                          )
                        )
                          AND b.updated_at<now()-interval '5 minutes'
                        ORDER BY b.updated_at,c.chunk_no LIMIT 500
                        """
                    )
                )
                from app.services.send_inflight import reconcile_in_flight_reservations

                await reconcile_in_flight_reservations(connection)
                return [
                    RecoveryWork("batch", str(row["batch_no"]), None, str(row["category"]))
                    for row in batches.mappings()
                ] + [
                    RecoveryWork(
                        "chunk",
                        str(row["batch_no"]),
                        int(row["chunk_id"]),
                        str(row["category"]),
                    )
                    for row in chunks.mappings()
                ]
        finally:
            await engine.dispose()
