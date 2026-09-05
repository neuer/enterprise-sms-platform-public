"""停滞批次/分片扫描；submitted、uncertain 与 unknown_terminal 永不进入结果集。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.reconcile import RecoveryWork
from app.settings import Settings, get_settings


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
                        WITH stale_invoking AS (
                          UPDATE sms_vendor_attempt
                          SET outcome='uncertain', updated_at=now()
                          WHERE outcome='invoking'
                            AND invoke_started_at < now() - interval '15 minutes'
                          RETURNING chunk_id
                        ), stale_chunks AS (
                          UPDATE sms_chunk c SET
                            status='uncertain',
                            uncertain_since=COALESCE(c.submitting_since,now()),
                            submitting_since=NULL
                          FROM sms_batch b
                          WHERE b.id=c.batch_id
                            AND b.status IN ('queued','sending')
                            AND (
                              (
                                c.status='submitting'
                                AND c.submitting_since<now()-interval '5 minutes'
                              )
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
