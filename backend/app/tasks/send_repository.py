"""发送 worker 的分片、状态迁移与受控解密仓储。"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.bounded_executor import ExecutorBackpressure, run_bounded
from app.core.runtime_resources import database_engine
from app.services.callback_repository import enqueue_batch_finished
from app.services.category import queue_for_category
from app.services.crypto import CryptoService
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.report_projection import NO_REPORT_EVIDENCE
from app.services.vendor_test_budget import (
    LIVE_TEST_DAILY_SEGMENT_LIMIT,
    SubmissionClaim,
    SubmissionClaimStatus,
    current_live_test_time,
    live_test_usage_window,
    settle_live_test_attempt,
)
from app.services.vendor_test_pause import pause_vendor_test_agent_stale
from app.settings import Settings, get_settings
from app.tasks import celery_app
from app.tasks.send import (
    ChunkPayload,
)
from app.tasks.send_repository_failover import VendorFailoverMixin
from app.tasks.send_repository_payload import ChunkPayloadMixin
from app.tasks.send_repository_split import (
    complete_vendor_split,
    retry_capacity_blocked_splits,
)
from app.vendor.codes import DELAYED_RETRY_EXHAUSTED, DELAYED_RETRY_LIMIT
from app.vendor.identifiers import vendor_identifier_pseudonym
from app.vendor.routing import (
    VendorRecord,
    default_vendor_registry,
)

# 厂商尝试与拆分子片已拆到 send_repository_* 模块；调用方继续从这里导入。
__all__ = ["SqlChunkStore", "complete_vendor_split", "retry_capacity_blocked_splits"]

LOGGER = logging.getLogger(__name__)


_VENDOR_CRITICAL_PAUSE_SCRIPT = """
local gen = redis.call('INCR', 'ratelimit:queue:paused:generation')
redis.call('SET', KEYS[1], ARGV[1] .. ':' .. gen)
redis.call('SET', KEYS[2], ARGV[1] .. ':' .. gen)
return 1
"""


class SqlChunkStore(ChunkPayloadMixin, VendorFailoverMixin):
    """PostgreSQL 是分片事实源；Redis 仅保存队列暂停开关。"""

    def __init__(
        self,
        crypto: CryptoService,
        settings: Settings | None = None,
        redis: Any | None = None,
        registry: tuple[VendorRecord, ...] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto
        self.redis: Any = redis or Redis.from_url(
            self.settings.redis_control_url,
            decode_responses=True,
        )
        self.registry = registry or default_vendor_registry()
        self.adapter_ids = frozenset(item.adapter for item in self.registry if item.adapter)

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    @staticmethod
    def lane_for(category: str) -> str:
        return queue_for_category(category)

    @staticmethod
    async def _enqueue_retry(chunk_id: int, lane: str, countdown: int) -> None:
        """队列不可用时保留 retry_not_before 事实，由 reconcile 兜底重投。"""

        try:
            await run_bounded(
                celery_app.send_task,
                "app.tasks.send.process_chunk",
                args=[chunk_id],
                queue=lane,
                countdown=countdown,
                ignore_result=True,
                timeout_s=3,
            )
        except (ExecutorBackpressure, TimeoutError) as error:
            LOGGER.warning(
                "chunk retry enqueue deferred to reconciler",
                extra={
                    "chunk_id": chunk_id,
                    "error_type": type(error).__name__,
                },
            )

    async def load_market_window(self) -> str:
        """外呼前读取权威营销窗口；不可确认时停止发送。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='market_send_window'")
                )
                value = result.scalar_one_or_none()
                if value is None:
                    raise RuntimeError("market send window unavailable")
                return str(value)
        finally:
            await engine.dispose()

    async def load_worker_config(self) -> tuple[int, int, int]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key, value FROM sys_config WHERE key IN (
                          'vendor_batch_size','vendor_qps','reserved_realtime_qps'
                        )
                        """
                    )
                )
                values = {str(row["key"]): int(row["value"]) for row in result.mappings()}
                return (
                    values.get("vendor_batch_size", 500),
                    values.get("vendor_qps", 5),
                    values.get("reserved_realtime_qps", 2),
                )
        finally:
            await engine.dispose()

    async def is_paused(self, lane: str) -> bool:
        critical = await self.redis.mget(
            ["queue:paused:realtime", "queue:paused:bulk"]
        )
        if any(value is not None for value in critical):
            return True
        agent_stale = (
            await self.redis.get("queue:paused:vendor-test-agent-stale:realtime"),
            await self.redis.get("queue:paused:vendor-test-agent-stale:bulk"),
        )
        if any(value is not None for value in agent_stale):
            return True
        daily = await self.redis.get(f"queue:paused:vendor-test-daily:{lane}")
        return daily is not None

    async def pause_daily_limit(
        self,
        lane: str,
        reset_at: datetime,
        *,
        now: datetime | None = None,
    ) -> None:
        """设置独立的日上限暂停键，不触碰 critical/manual 暂停。"""

        current = now or datetime.now(UTC)
        ttl = max(1, math.ceil((reset_at - current).total_seconds()))
        await self.redis.set(
            f"queue:paused:vendor-test-daily:{lane}",
            "daily_limit",
            ex=ttl,
        )

    async def pause_control_agent_stale(self) -> None:
        """设置独立 critical pause，绝不覆盖厂商、人工或每日暂停键。"""

        await pause_vendor_test_agent_stale(self.redis)

    async def release_unsent(self, chunk_id: int) -> None:
        """确认尚未调用厂商时把 submitting 放回 retrying，允许自动重试。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                released = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='retrying',
                          vendor_msg='submit aborted before vendor call',
                          submitting_since=NULL,retry_not_before=now()
                        WHERE id=:id AND status='submitting'
                        RETURNING id
                        """
                    ),
                    {"id": chunk_id},
                )
                if released.scalar_one_or_none() is not None:
                    await settle_live_test_attempt(connection, chunk_id, "released")
        finally:
            await engine.dispose()

    async def release_control_claim(self, chunk_id: int) -> None:
        """厂商调用前状态失效时释放占额，分片保持可重试。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                released = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='retrying',vendor_code=NULL,
                          vendor_msg='vendor control state unavailable',
                          submitting_since=NULL,retry_not_before=now()
                        WHERE id=:id AND status='submitting'
                        RETURNING id
                        """
                    ),
                    {"id": chunk_id},
                )
                if released.scalar_one_or_none() is not None:
                    await settle_live_test_attempt(connection, chunk_id, "released")
        finally:
            await engine.dispose()

    @staticmethod
    async def _enqueue_chunk_ready(
        connection: AsyncConnection,
        chunk_ids: list[int],
        lane: str,
    ) -> None:
        """在批次事务内登记分片发送事件；同 chunk 重复规划必须合同不变。"""

        for chunk_id in chunk_ids:
            dedup_key = f"chunk.ready:{chunk_id}"
            await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="chunk.ready",
                    aggregate_type="sms_chunk",
                    aggregate_id=str(chunk_id),
                    task_name="app.tasks.send.process_chunk",
                    queue=lane,
                    args=(chunk_id,),
                    dedup_key=dedup_key,
                ),
            )
            await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state='pending',
                      next_attempt_at=now(),
                      attempts=0,
                      failure_count=0,
                      lease_id=NULL,
                      lease_expires_at=NULL,
                      last_error=NULL,
                      completed_at=NULL,
                      updated_at=now()
                    WHERE dedup_key=:dedup_key
                      AND event_type='chunk.ready'
                      AND state IN ('completed','dead')
                    """
                ),
                {"dedup_key": dedup_key},
            )

    async def mark_submitting(
        self,
        chunk_id: int,
        expected_retry_count: int,
    ) -> bool:
        changed = await self._update_chunk(
            "WITH claimed AS ("
            "UPDATE sms_chunk AS c SET status='submitting',submitting_since=now(),"
            "retry_not_before=NULL "
            "WHERE c.id=:id "
            "AND c.status IN ('pending','retrying') "
            "AND c.retry_count=:expected_retry_count AND EXISTS ("
            "SELECT 1 FROM sms_batch b WHERE b.id=c.batch_id "
            "AND b.status IN ('queued','sending')) "
            "AND (c.retry_not_before IS NULL OR c.retry_not_before<=now()) "
            "RETURNING c.batch_id) "
            "UPDATE sms_batch SET updated_at=now() "
            "WHERE id IN (SELECT batch_id FROM claimed)",
            {"id": chunk_id, "expected_retry_count": expected_retry_count},
        )
        return changed == 1

    async def claim_submission(
        self,
        chunk_id: int,
        expected_retry_count: int,
        segments: int,
        *,
        enforce_live_test_budget: bool,
    ) -> SubmissionClaim:
        """原子锁定分片，并在真实联调时同时预留当日计费条。"""

        if segments < 1:
            raise ValueError("segments must be positive")
        if not enforce_live_test_budget:
            claimed = await self.mark_submitting(chunk_id, expected_retry_count)
            return SubmissionClaim(
                SubmissionClaimStatus.CLAIMED if claimed else SubmissionClaimStatus.STALE
            )

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                if bool(getattr(self.settings, "vendor_live_test", False)):
                    payload = await self._payload(connection, chunk_id)
                    if payload.denied_recipient_count:
                        return SubmissionClaim(SubmissionClaimStatus.STALE)
                chunk_result = await connection.execute(
                    text(
                        """
                        SELECT c.id,c.batch_id,c.vendor_attempt_count
                        FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.id=:id
                          AND c.status IN ('pending','retrying')
                          AND c.retry_count=:expected_retry_count
                          AND b.status IN ('queued','sending')
                          AND (c.retry_not_before IS NULL OR c.retry_not_before<=now())
                        FOR UPDATE OF c,b
                        """
                    ),
                    {"id": chunk_id, "expected_retry_count": expected_retry_count},
                )
                chunk = chunk_result.mappings().one_or_none()
                if chunk is None:
                    return SubmissionClaim(SubmissionClaimStatus.STALE)

                attempt_no = int(chunk["vendor_attempt_count"]) + 1
                budget = await self._reserve_submission_budget(
                    connection, chunk_id, segments, attempt_no
                )
                if budget.status is not SubmissionClaimStatus.CLAIMED:
                    return budget
                transition = await connection.execute(
                    text("""
                    UPDATE sms_chunk SET status='submitting',submitting_since=now(),
                      retry_not_before=NULL,vendor_attempt_count=vendor_attempt_count+1
                    WHERE id=:id AND status IN ('pending','retrying')
                      AND retry_count=:expected_retry_count RETURNING id
                """),
                    {"id": chunk_id, "expected_retry_count": expected_retry_count},
                )
                if transition.scalar_one_or_none() is None:
                    raise RuntimeError("submission claim lost cas")
                await connection.execute(
                    text("UPDATE sms_batch SET updated_at=now() WHERE id=:batch_id"),
                    {"batch_id": int(chunk["batch_id"])},
                )
                return SubmissionClaim(SubmissionClaimStatus.CLAIMED)
        finally:
            await engine.dispose()

    async def _reserve_submission_budget(
        self,
        connection: AsyncConnection,
        chunk_id: int,
        segments: int,
        attempt_no: int,
    ) -> SubmissionClaim:
        """在已锁定分片的同一事务，为每个真实联调 attempt 独立预留预算。"""

        if segments < 1 or attempt_no < 1:
            raise ValueError("submission budget identity invalid")
        usage_date, reset_at = live_test_usage_window(current_live_test_time())
        await connection.execute(
            text(
                """
                INSERT INTO vendor_test_daily_usage(usage_date)
                VALUES (:usage_date) ON CONFLICT (usage_date) DO NOTHING
                """
            ),
            {"usage_date": usage_date},
        )
        usage_result = await connection.execute(
            text(
                """
                SELECT in_flight_segments,confirmed_segments,uncertain_segments
                FROM vendor_test_daily_usage
                WHERE usage_date=:usage_date FOR UPDATE
                """
            ),
            {"usage_date": usage_date},
        )
        usage = usage_result.mappings().one()
        total = sum(
            int(usage[key])
            for key in (
                "in_flight_segments",
                "confirmed_segments",
                "uncertain_segments",
            )
        )
        if total + segments > LIVE_TEST_DAILY_SEGMENT_LIMIT:
            return SubmissionClaim(SubmissionClaimStatus.DAILY_LIMIT, reset_at)

        await connection.execute(
            text(
                """
                INSERT INTO vendor_test_send_attempt(
                  usage_date,chunk_id,attempt_no,segments,status
                ) VALUES (
                  :usage_date,:chunk_id,:attempt_no,:segments,'reserved'
                )
                """
            ),
            {
                "usage_date": usage_date,
                "chunk_id": chunk_id,
                "attempt_no": int(attempt_no),
                "segments": segments,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE vendor_test_daily_usage SET
                  in_flight_segments=in_flight_segments+:segments,
                  updated_at=now()
                WHERE usage_date=:usage_date
                """
            ),
            {"usage_date": usage_date, "segments": segments},
        )
        return SubmissionClaim(SubmissionClaimStatus.CLAIMED)

    async def mark_submitted(self, chunk_id: int, task_id: str) -> None:
        task_pseudonym = vendor_identifier_pseudonym(
            self.crypto,
            task_id,
            domain="vendor-task-id",
        )
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                submitted = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='submitted', vendor_task_id=:task_id,
                          submitted_at=now(),submitting_since=NULL,retry_not_before=NULL
                        WHERE id=:id AND status='submitting'
                        RETURNING id
                        """
                    ),
                    {"id": chunk_id, "task_id": task_pseudonym},
                )
                if submitted.scalar_one_or_none() is None:
                    return
                await settle_live_test_attempt(connection, chunk_id, "confirmed")
                await connection.execute(
                    text(
                        "UPDATE sms_message SET status='sent' WHERE chunk_id=:id "
                        f"AND status='pending' AND {NO_REPORT_EVIDENCE}"
                    ),
                    {"id": chunk_id},
                )
        finally:
            await engine.dispose()

    async def _finalize_failed_messages(
        self,
        connection: AsyncConnection,
        chunk_id: int,
        batch_id: int,
    ) -> None:
        """在已锁定批次的事务内完成消息失败聚合与终态回调。"""

        from app.services.uncertain_resolution import reevaluate_confirmed_unused_batch

        messages = await connection.execute(
            text(
                "UPDATE sms_message SET status='failed' WHERE chunk_id=:id "
                f"AND status IN ('pending','sent') AND {NO_REPORT_EVIDENCE}"
            ),
            {"id": chunk_id},
        )
        if messages.rowcount == 0:
            await reevaluate_confirmed_unused_batch(connection, batch_id)
            return
        aggregate = await connection.execute(
            text(
                """
                UPDATE sms_batch b SET
                  delivered=s.delivered,failed=s.failed,unknown_cnt=s.unknown_cnt,
                  active_message_count=s.active,
                  active_message_count_token=gen_random_uuid(),
                  status=CASE
                    WHEN b.status='completed_unknown' THEN 'completed_unknown'
                    WHEN s.active=0 THEN 'completed'
                    ELSE b.status
                  END,
                  updated_at=now()
                FROM (
                  SELECT batch_id,
                    count(*) FILTER (WHERE status='delivered') delivered,
                    count(*) FILTER (WHERE status='failed') failed,
                    count(*) FILTER (WHERE status='unknown') unknown_cnt,
                    count(*) FILTER (WHERE status IN ('pending','sent')) active
                  FROM sms_message WHERE batch_id=:batch_id GROUP BY batch_id
                ) s
                WHERE b.id=s.batch_id
                RETURNING b.id,b.status
                """
            ),
            {"batch_id": batch_id},
        )
        batch = aggregate.mappings().one()
        if str(batch["status"]) == "completed":
            await enqueue_batch_finished(connection, int(batch["id"]))
            from app.services.send_inflight import request_inflight_release_for_batch

            await request_inflight_release_for_batch(
                connection,
                batch_id=int(batch["id"]),
                reason="batch-completed",
            )

        await reevaluate_confirmed_unused_batch(connection, batch_id)

    async def mark_failed(self, chunk_id: int, code: int, message: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                candidate = await connection.execute(
                    text("SELECT batch_id FROM sms_chunk WHERE id=:id "
                         "AND status='submitting' FOR UPDATE"),
                    {"id": chunk_id},
                )
                batch_id = candidate.scalar_one_or_none()
                if batch_id is None:
                    return
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(batch_id)},
                )
                failed = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='failed',vendor_code=:code,
                          vendor_msg=:message,submitting_since=NULL,retry_not_before=NULL
                        WHERE id=:id AND status='submitting'
                        RETURNING batch_id
                        """
                    ),
                    {"id": chunk_id, "code": code, "message": message},
                )
                transitioned_batch_id = failed.scalar_one_or_none()
                if transitioned_batch_id is None:
                    return
                await settle_live_test_attempt(connection, chunk_id, "released")
                await self._finalize_failed_messages(
                    connection,
                    chunk_id,
                    int(transitioned_batch_id),
                )
        finally:
            await engine.dispose()

    async def reject_disallowed_recipient(
        self,
        chunk_id: int,
        denied_count: int,
    ) -> None:
        """在未创建厂商 attempt 前终结不符合真实联调白名单的分片。"""

        if denied_count < 1:
            raise ValueError("denied_count must be positive")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                candidate = await connection.execute(
                    text(
                        """
                        SELECT c.batch_id FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.id=:id
                          AND c.status IN ('pending','retrying','failover_pending')
                          AND b.status IN ('queued','sending')
                        """
                    ),
                    {"id": chunk_id},
                )
                batch_id = candidate.scalar_one_or_none()
                if batch_id is None:
                    return
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(batch_id)},
                )
                transitioned = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='failed',vendor_code=NULL,
                          vendor_msg=:message,submitting_since=NULL,retry_not_before=NULL
                        WHERE id=:id AND status IN ('pending','retrying','failover_pending')
                        RETURNING batch_id
                        """
                    ),
                    {
                        "id": chunk_id,
                        "message": (f"live-test recipient denied: count={denied_count}"),
                    },
                )
                transitioned_batch_id = transitioned.scalar_one_or_none()
                if transitioned_batch_id is None:
                    return
                await self._finalize_failed_messages(
                    connection,
                    chunk_id,
                    int(transitioned_batch_id),
                )
        finally:
            await engine.dispose()

    async def defer_daily_limit(
        self,
        chunk_id: int,
        lane: str,
        reset_at: datetime,
    ) -> None:
        """日预算满时保持未下发语义，并仅调度到下一上海自然日。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk c SET status=CASE WHEN c.status='failover_pending'
                          THEN 'failover_pending' ELSE 'retrying' END,vendor_code=NULL,
                          retry_not_before=:reset_at
                        FROM sms_batch b
                        WHERE c.id=:id AND c.status IN ('pending','retrying','failover_pending')
                          AND b.id=c.batch_id AND b.status IN ('queued','sending')
                        RETURNING b.category
                        """
                    ),
                    {"id": chunk_id, "reset_at": reset_at},
                )
                category = result.scalar_one_or_none()
        finally:
            await engine.dispose()
        if category is not None:
            actual_lane = self.lane_for(str(category))
            if actual_lane != lane:
                raise RuntimeError("chunk lane changed before daily-limit deferral")
            countdown = max(
                1,
                math.ceil((reset_at - current_live_test_time()).total_seconds()),
            )
            await self._enqueue_retry(chunk_id, actual_lane, countdown)

    async def mark_uncertain(self, chunk_id: int) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                transitioned = await connection.execute(
                    text(
                        "UPDATE sms_chunk SET status='uncertain',"
                        "uncertain_since=COALESCE(submitting_since,now()),"
                        "submitting_since=NULL,retry_not_before=NULL "
                        "WHERE id=:id AND status='submitting' RETURNING id"
                    ),
                    {"id": chunk_id},
                )
                if transitioned.scalar_one_or_none() is not None:
                    await settle_live_test_attempt(connection, chunk_id, "uncertain")
        finally:
            await engine.dispose()

    async def schedule_retry(
        self,
        chunk_id: int,
        code: int,
        expected_retry_count: int,
        delay_s: int,
    ) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        "UPDATE sms_chunk c SET status='retrying',vendor_code=:code,"
                        "retry_count=retry_count+1,submitting_since=NULL,"
                        "retry_not_before=now()+make_interval(secs=>:delay_s) "
                        "FROM sms_batch b "
                        "WHERE c.id=:id AND c.status='submitting' "
                        "AND c.retry_count=:expected_retry_count "
                        "AND c.retry_count<5 AND b.id=c.batch_id "
                        "RETURNING b.category"
                    ),
                    {
                        "id": chunk_id,
                        "code": code,
                        "expected_retry_count": expected_retry_count,
                        "delay_s": delay_s,
                    },
                )
                category = changed.scalar_one_or_none()
                if category is None:
                    return False
                await settle_live_test_attempt(connection, chunk_id, "released")
        finally:
            await engine.dispose()
        lane = self.lane_for(str(category))
        await self._enqueue_retry(chunk_id, lane, delay_s)
        return True

    async def delay(self, chunk_id: int, code: int, delay_s: int) -> None:
        category = None
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk c SET status='retrying',vendor_code=:code,
                          retry_count=retry_count+1,
                          submitting_since=NULL,
                          retry_not_before=now()+make_interval(secs=>:delay_s)
                        FROM sms_batch b
                        WHERE c.id=:id AND c.status='submitting' AND b.id=c.batch_id
                          AND c.retry_count<:retry_limit
                        RETURNING b.category
                        """
                    ),
                    {"id": chunk_id, "code": code, "delay_s": delay_s,
                     "retry_limit": DELAYED_RETRY_LIMIT},
                )
                category = result.scalar_one_or_none()
                if category is not None:
                    await settle_live_test_attempt(connection, chunk_id, "released")
                else:
                    candidate = await connection.execute(
                        text(
                            "SELECT batch_id FROM sms_chunk "
                            "WHERE id=:id AND status='submitting'"
                        ),
                        {"id": chunk_id},
                    )
                    batch_id = candidate.scalar_one_or_none()
                    if batch_id is not None:
                        await connection.execute(
                            text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                            {"id": int(batch_id)},
                        )
                        failed = await connection.execute(
                            text(
                                "UPDATE sms_chunk SET status='failed',vendor_code=:code,"
                                "vendor_msg=:reason,"
                                "submitting_since=NULL,retry_not_before=NULL "
                                "WHERE id=:id AND status='submitting' "
                                "RETURNING batch_id"
                            ),
                            {"id": chunk_id, "code": code,
                             "reason": DELAYED_RETRY_EXHAUSTED},
                        )
                        transitioned_batch_id = failed.scalar_one_or_none()
                        if transitioned_batch_id is not None:
                            await settle_live_test_attempt(
                                connection,
                                chunk_id,
                                "released",
                            )
                            await self._finalize_failed_messages(
                                connection,
                                chunk_id,
                                int(transitioned_batch_id),
                            )
        finally:
            await engine.dispose()
        if category is not None:
            lane = self.lane_for(str(category))
            await self._enqueue_retry(chunk_id, lane, delay_s)

    async def balance_blocked(self, batch_id: int, chunk_id: int) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                transitioned = await connection.execute(
                    text(
                        "UPDATE sms_chunk SET status='retrying',vendor_code=999,"
                        "submitting_since=NULL,retry_not_before=now() "
                        "WHERE id=:id AND status='submitting' RETURNING id"
                    ),
                    {"id": chunk_id},
                )
                if transitioned.scalar_one_or_none() is None:
                    return
                await settle_live_test_attempt(connection, chunk_id, "released")
                await connection.execute(
                    text("UPDATE sms_batch SET status='balance_blocked' WHERE id=:id"),
                    {"id": batch_id},
                )
        finally:
            await engine.dispose()

    async def pause_blocked(self, chunk_id: int, code: int) -> None:
        """熔断码把在途 chunk 回退 retrying，恢复队列后由 reconcile 重投。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                transitioned = await connection.execute(
                    text(
                        "UPDATE sms_chunk SET status='retrying',vendor_code=:code,"
                        "submitting_since=NULL,retry_not_before=now() "
                        "WHERE id=:id AND status='submitting' RETURNING id"
                    ),
                    {"id": chunk_id, "code": code},
                )
                if transitioned.scalar_one_or_none() is None:
                    return
                await settle_live_test_attempt(connection, chunk_id, "released")
        finally:
            await engine.dispose()

    async def pause_queues(self, code: int) -> None:
        result = await self.redis.eval(
            _VENDOR_CRITICAL_PAUSE_SCRIPT,
            2,
            "queue:paused:realtime",
            "queue:paused:bulk",
            str(code),
        )
        if result != 1:
            raise RuntimeError("vendor critical pause was not persisted")

    async def split_once(self, chunk: ChunkPayload) -> list[ChunkPayload]:
        if len(chunk.phones) < 2:
            return []
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                child_ids = await complete_vendor_split(connection, chunk)
                return [await self._payload(connection, child_id) for child_id in child_ids]
        finally:
            await engine.dispose()

    async def _update_chunk(self, statement: str, values: dict[str, Any]) -> int:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), values)
                return int(result.rowcount)
        finally:
            await engine.dispose()



