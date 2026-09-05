"""发送 worker 的分片、状态迁移与受控解密仓储。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.bounded_executor import ExecutorBackpressure, run_bounded
from app.core.runtime_resources import database_engine
from app.services.callback_repository import enqueue_batch_finished
from app.services.category import queue_for_category
from app.services.crypto import CryptoService, EncryptionContext
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.vendor_test_budget import (
    LIVE_TEST_DAILY_SEGMENT_LIMIT,
    SubmissionClaim,
    SubmissionClaimStatus,
    current_live_test_time,
    live_test_usage_window,
    settle_live_test_attempt,
)
from app.services.vendor_test_pause import pause_vendor_test_agent_stale
from app.services.vendor_test_recipient_repository import (
    lock_vendor_test_recipient_maintenance,
)
from app.settings import Settings, get_settings
from app.tasks import celery_app
from app.tasks.send import (
    ChunkPayload,
    FinalizeKind,
    FinalizeReport,
    classify_finalize_conflict,
)
from app.vendor.identifiers import vendor_identifier_pseudonym
from app.vendor.routing import VendorAttempt

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VendorAttemptRow:
    id: int
    generation: int
    vendor_id: str
    outcome: str


class _FinalizeRollback(Exception):
    """chunk CAS 失败时回滚已写入的 attempt，禁止提交半成品。"""

    def __init__(self, report: FinalizeReport) -> None:
        super().__init__(report.kind.value)
        self.report = report


_VENDOR_CRITICAL_PAUSE_SCRIPT = """
local gen = redis.call('INCR', 'ratelimit:queue:paused:generation')
redis.call('SET', KEYS[1], ARGV[1] .. ':' .. gen)
redis.call('SET', KEYS[2], ARGV[1] .. ':' .. gen)
return 1
"""


class SqlChunkStore:
    """PostgreSQL 是分片事实源；Redis 仅保存队列暂停开关。"""

    def __init__(
        self,
        crypto: CryptoService,
        settings: Settings | None = None,
        redis: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto
        self.redis: Any = redis or Redis.from_url(
            self.settings.redis_control_url,
            decode_responses=True,
        )

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

    async def _payload(
        self,
        connection: AsyncConnection,
        chunk_id: int,
    ) -> ChunkPayload:
        enforce_recipient_guard = bool(getattr(self.settings, "vendor_live_test", False))
        if enforce_recipient_guard:
            await lock_vendor_test_recipient_maintenance(connection)
        result = await connection.execute(
            text(
                """
                SELECT c.id chunk_id, c.batch_id, c.custom_id,c.retry_count,
                       COALESCE(c.selected_vendor,'zhihui') selected_vendor,
                       COALESCE(c.route_generation,1) route_generation,
                       trim(b.batch_no) batch_no,b.send_content_enc,b.sign_name,
                       t.vendor_template_id
                FROM sms_chunk c
                JOIN sms_batch b ON b.id=c.batch_id
                LEFT JOIN sms_template t ON t.id=b.template_id
                WHERE c.id=:chunk_id
                """
            ),
            {"chunk_id": chunk_id},
        )
        row = result.mappings().one()
        phones_result = await connection.execute(
            text(
                """
                SELECT m.phone_enc, trim(m.phone_hmac) phone_hmac, m.key_version
                FROM sms_message m
                WHERE m.chunk_id=:chunk_id ORDER BY m.id, m.created_at
                """
            ),
            {"chunk_id": chunk_id},
        )
        phone_rows = list(phones_result.mappings())
        decrypted_phones = tuple(
            self.crypto.decrypt_phone(
                bytes(item["phone_enc"]),
                int(item["key_version"]),
                str(item["phone_hmac"]),
            )
            for item in phone_rows
        )
        denied_recipient_count = 0
        if enforce_recipient_guard:
            for phone in decrypted_phones:
                candidates = self.crypto.hmac_candidates(phone)
                conditions: list[str] = []
                parameters: dict[str, object] = {}
                for index, (version, digest) in enumerate(candidates.items()):
                    conditions.append(
                        f"(key_version=:version_{index} AND phone_hmac=:hmac_{index})"
                    )
                    parameters[f"version_{index}"] = version
                    parameters[f"hmac_{index}"] = digest
                allowed = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM vendor_test_recipient "
                        "WHERE status='active' AND (" + " OR ".join(conditions) + "))"
                    ),
                    parameters,
                )
                if not bool(allowed.scalar_one()):
                    denied_recipient_count += 1
        phones = () if denied_recipient_count else decrypted_phones
        return ChunkPayload(
            chunk_id=int(row["chunk_id"]),
            batch_id=int(row["batch_id"]),
            custom_id=str(row["custom_id"]).strip(),
            phones=phones,
            content=self.crypto.decrypt_bound_packed_text(
                bytes(row["send_content_enc"]),
                EncryptionContext(
                    domain="sms-content",
                    table="sms_batch",
                    column="send_content_enc",
                    object_id=str(row["batch_no"]),
                ),
            ),
            template_id=(
                str(row["vendor_template_id"]) if row["vendor_template_id"] is not None else ""
            ),
            sign_name=str(row["sign_name"] or ""),
            retry_count=int(row["retry_count"]),
            denied_recipient_count=denied_recipient_count,
            selected_vendor=str(row.get("selected_vendor") or "zhihui"),
            route_generation=max(1, int(row.get("route_generation") or 1)),
        )

    async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                status_result = await connection.execute(
                    text(
                        """
                        SELECT c.status, b.category, b.status batch_status
                        FROM sms_chunk c
                        JOIN sms_batch b ON b.id=c.batch_id
                        WHERE c.id=:chunk_id
                          AND b.status IN ('queued','sending')
                          AND (c.status='pending' OR (
                            c.status='retrying' AND c.retry_not_before<=now()
                          ))
                        """
                    ),
                    {"chunk_id": chunk_id},
                )
                row = status_result.mappings().one_or_none()
                if (
                    row is None
                    or row["status"] not in {"pending", "retrying"}
                    or row["batch_status"] not in {"queued", "sending"}
                ):
                    return None
                return (
                    await self._payload(connection, chunk_id),
                    self.lane_for(str(row["category"])),
                )
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

    async def prepare_chunks(
        self,
        batch_no: str,
        batch_size: int,
    ) -> tuple[list[int], str]:
        """规划分片并登记 child Outbox；事务内只处理元数据，不解密手机号。"""

        if batch_size < 1:
            raise ValueError("vendor_batch_size must be positive")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                batch_result = await connection.execute(
                    text(
                        "SELECT id, category, status, app_id, "
                        "usage_reservation_id, segments FROM sms_batch "
                        "WHERE batch_no=:batch_no "
                        "AND status IN ('queued','sending') FOR UPDATE"
                    ),
                    {"batch_no": batch_no},
                )
                batch = batch_result.mappings().one_or_none()
                if batch is None or str(batch["status"]) not in {"queued", "sending"}:
                    raise RuntimeError("批次状态不允许发送")
                batch_id = int(batch["id"])
                existing = await connection.execute(
                    text(
                        """
                        SELECT id FROM sms_chunk WHERE batch_id=:batch_id
                          AND (status='pending' OR (
                            status='retrying' AND retry_not_before<=now()
                          )) ORDER BY chunk_no
                        """
                    ),
                    {"batch_id": batch_id},
                )
                chunk_ids = [int(value) for value in existing.scalars()]
                if not chunk_ids:
                    any_chunks = await connection.execute(
                        text("SELECT count(*) FROM sms_chunk WHERE batch_id=:batch_id"),
                        {"batch_id": batch_id},
                    )
                    if int(any_chunks.scalar_one()) > 0:
                        if str(batch["status"]) == "queued":
                            await connection.execute(
                                text(
                                    "UPDATE sms_batch SET status='sending',updated_at=now() "
                                    "WHERE id=:id AND status='queued'"
                                ),
                                {"id": batch_id},
                            )
                        return [], self.lane_for(str(batch["category"]))
                    messages = await connection.execute(
                        text(
                            """
                            SELECT id, created_at FROM sms_message
                            WHERE batch_id=:batch_id AND chunk_id IS NULL
                            ORDER BY id, created_at
                            """
                        ),
                        {"batch_id": batch_id},
                    )
                    message_rows = list(messages.mappings())
                    for offset in range(0, len(message_rows), batch_size):
                        group = message_rows[offset : offset + batch_size]
                        chunk_no = offset // batch_size + 1
                        custom_id = f"{batch_no[:24]}{chunk_no:08d}"
                        inserted = await connection.execute(
                            text(
                                """
                                INSERT INTO sms_chunk (
                                  batch_id, chunk_no, custom_id, phone_count
                                ) VALUES (:batch_id,:chunk_no,:custom_id,:phone_count)
                                RETURNING id
                                """
                            ),
                            {
                                "batch_id": batch_id,
                                "chunk_no": chunk_no,
                                "custom_id": custom_id,
                                "phone_count": len(group),
                            },
                        )
                        chunk_id = int(inserted.scalar_one())
                        chunk_ids.append(chunk_id)
                        segment_count = int(batch["segments"] or 0) * len(group)
                        await connection.execute(
                            text(
                                """
                                INSERT INTO usage_chunk_allocation (
                                  chunk_id, batch_id, reservation_id,
                                  recipient_count, segment_count,
                                  request_count, app_id
                                ) VALUES (
                                  :chunk_id, :batch_id, :reservation_id,
                                  :recipients, :segments,
                                  :requests, :app_id
                                )
                                ON CONFLICT (chunk_id) DO NOTHING
                                """
                            ),
                            {
                                "chunk_id": chunk_id,
                                "batch_id": batch_id,
                                "reservation_id": batch["usage_reservation_id"],
                                "recipients": len(group),
                                "segments": segment_count,
                                "requests": 1 if chunk_no == 1 else 0,
                                "app_id": batch["app_id"],
                            },
                        )
                        await connection.execute(
                            text(
                                """
                                UPDATE sms_message SET chunk_id=:chunk_id
                                WHERE id=:id AND created_at=:created_at
                                """
                            ),
                            [
                                {
                                    "chunk_id": chunk_id,
                                    "id": item["id"],
                                    "created_at": item["created_at"],
                                }
                                for item in group
                            ],
                        )
                    await connection.execute(
                        text("UPDATE sms_batch SET status='sending',updated_at=now() WHERE id=:id"),
                        {"id": batch_id},
                    )
                    from app.services.send_inflight import materialize_in_flight_reservation

                    actual_chunks = int(
                        (
                            await connection.execute(
                                text(
                                    "SELECT count(*) FROM sms_chunk WHERE batch_id=:batch_id"
                                ),
                                {"batch_id": batch_id},
                            )
                        ).scalar_one()
                    )
                    app_limit = 200
                    if batch["app_id"] is not None:
                        limit_row = await connection.execute(
                            text(
                                "SELECT max_in_flight_chunks FROM app WHERE id=:app_id"
                            ),
                            {"app_id": batch["app_id"]},
                        )
                        loaded_limit = limit_row.scalar_one_or_none()
                        if loaded_limit is not None:
                            app_limit = max(1, int(loaded_limit))
                    await materialize_in_flight_reservation(
                        connection,
                        batch_id=batch_id,
                        actual_chunks=actual_chunks,
                        limit=app_limit,
                    )
                elif str(batch["status"]) == "queued":
                    await connection.execute(
                        text(
                            "UPDATE sms_batch SET status='sending',updated_at=now() "
                            "WHERE id=:id AND status='queued'"
                        ),
                        {"id": batch_id},
                    )
                lane = self.lane_for(str(batch["category"]))
                await self._enqueue_chunk_ready(connection, chunk_ids, lane)
                return chunk_ids, lane
        finally:
            await engine.dispose()

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

        usage_date, reset_at = live_test_usage_window(current_live_test_time())
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                chunk_result = await connection.execute(
                    text(
                        """
                        SELECT c.id,c.batch_id
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

                transition = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET
                          status='submitting',submitting_since=now(),
                          retry_not_before=NULL,
                          vendor_attempt_count=vendor_attempt_count+1
                        WHERE id=:id
                          AND status IN ('pending','retrying')
                          AND retry_count=:expected_retry_count
                        RETURNING vendor_attempt_count
                        """
                    ),
                    {"id": chunk_id, "expected_retry_count": expected_retry_count},
                )
                attempt_no = transition.scalar_one_or_none()
                if attempt_no is None:
                    return SubmissionClaim(SubmissionClaimStatus.STALE)
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
                await connection.execute(
                    text("UPDATE sms_batch SET updated_at=now() WHERE id=:batch_id"),
                    {"batch_id": int(chunk["batch_id"])},
                )
                return SubmissionClaim(SubmissionClaimStatus.CLAIMED)
        finally:
            await engine.dispose()

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
                    text("UPDATE sms_message SET status='sent' WHERE chunk_id=:id"),
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

        await connection.execute(
            text("UPDATE sms_message SET status='failed' WHERE chunk_id=:id"),
            {"id": chunk_id},
        )
        aggregate = await connection.execute(
            text(
                """
                UPDATE sms_batch b SET
                  delivered=s.delivered,failed=s.failed,unknown_cnt=s.unknown_cnt,
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

    async def mark_failed(self, chunk_id: int, code: int, message: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                candidate = await connection.execute(
                    text("SELECT batch_id FROM sms_chunk WHERE id=:id AND status='submitting'"),
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
                          AND c.status IN ('pending','retrying')
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
                        WHERE id=:id AND status IN ('pending','retrying')
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
                        UPDATE sms_chunk c SET status='retrying',vendor_code=NULL,
                          retry_not_before=:reset_at
                        FROM sms_batch b
                        WHERE c.id=:id AND c.status IN ('pending','retrying')
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
                          AND c.retry_count<8
                        RETURNING b.category
                        """
                    ),
                    {"id": chunk_id, "code": code, "delay_s": delay_s},
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
                                "vendor_msg='delayed retry exhausted',"
                                "submitting_since=NULL,retry_not_before=NULL "
                                "WHERE id=:id AND status='submitting' "
                                "RETURNING batch_id"
                            ),
                            {"id": chunk_id, "code": code},
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

    async def list_vendor_attempts(self, chunk_id: int) -> tuple[VendorAttempt, ...]:
        """按 generation 加载完整 attempt 历史，供跨任务路由。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT vendor_id, generation, outcome,
                                   safe_to_failover, vendor_code
                            FROM sms_vendor_attempt
                            WHERE chunk_id=:chunk_id
                            ORDER BY generation
                            """
                        ),
                        {"chunk_id": chunk_id},
                    )
                ).mappings()
                return tuple(
                    VendorAttempt(
                        str(row["vendor_id"]),
                        int(row["generation"]),
                        str(row["outcome"]),
                        bool(row["safe_to_failover"]),
                        int(row["vendor_code"])
                        if row["vendor_code"] is not None
                        else None,
                    )
                    for row in rows
                )
        finally:
            await engine.dispose()

    async def begin_vendor_invoke(
        self,
        chunk_id: int,
        *,
        vendor_id: str,
        adapter_id: str,
        reason: str,
    ) -> VendorAttemptRow:
        """在 HTTP 之前原子分配 generation 并写入 invoking。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT id FROM sms_chunk WHERE id=:id FOR UPDATE"),
                    {"id": chunk_id},
                )
                history = (
                    await connection.execute(
                        text(
                            """
                            SELECT id, generation, outcome
                            FROM sms_vendor_attempt
                            WHERE chunk_id=:chunk_id
                            ORDER BY generation
                            FOR UPDATE
                            """
                        ),
                        {"chunk_id": chunk_id},
                    )
                ).mappings().all()
                if any(
                    str(row["outcome"])
                    in {"submitted", "uncertain", "invoking", "inconsistent"}
                    for row in history
                ):
                    raise RuntimeError("vendor attempt already irreversible")
                next_generation = (
                    max((int(row["generation"]) for row in history), default=0) + 1
                )
                inserted = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_vendor_attempt (
                              chunk_id, vendor_id, generation, outcome,
                              adapter_id, routing_reason, invoke_started_at
                            ) VALUES (
                              :chunk_id, :vendor_id, :generation, 'invoking',
                              :adapter_id, :reason, now()
                            )
                            RETURNING id, generation, vendor_id, outcome
                            """
                        ),
                        {
                            "chunk_id": chunk_id,
                            "vendor_id": vendor_id,
                            "generation": next_generation,
                            "adapter_id": adapter_id,
                            "reason": reason[:64],
                        },
                    )
                ).mappings().one()
                await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET
                          selected_vendor=:vendor_id,
                          route_generation=:generation
                        WHERE id=:chunk_id
                        """
                    ),
                    {
                        "chunk_id": chunk_id,
                        "vendor_id": vendor_id,
                        "generation": next_generation,
                    },
                )
                return VendorAttemptRow(
                    int(inserted["id"]),
                    int(inserted["generation"]),
                    str(inserted["vendor_id"]),
                    str(inserted["outcome"]),
                )
        finally:
            await engine.dispose()

    async def finalize_vendor_attempt(
        self,
        attempt_id: int,
        chunk_id: int,
        *,
        expected_generation: int,
        result: str,
        vendor_task_id: str | None = None,
        vendor_code: int | None = None,
        safe_to_failover: bool = False,
        retry_delay_s: int | None = None,
        expected_retry_count: int | None = None,
        batch_id: int | None = None,
        balance_blocked: bool = False,
    ) -> FinalizeReport:
        """在同一事务内 CAS 终结 attempt 与 chunk，避免 submitted/invoking 撕裂。"""

        if result not in {
            "submitted",
            "uncertain",
            "retry_scheduled",
            "delayed",
            "paused",
            "rejected",
            "failed",
            "cancelled_before_invoke",
        }:
            raise ValueError("unsupported vendor finalize result")
        task_pseudonym = (
            vendor_identifier_pseudonym(
                self.crypto,
                vendor_task_id,
                domain="vendor-task-id",
            )
            if result == "submitted" and vendor_task_id
            else None
        )
        enqueue: tuple[int, str, int] | None = None
        report = FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                attempt = (
                    await connection.execute(
                        text(
                            """
                            SELECT id, chunk_id, generation, outcome
                            FROM sms_vendor_attempt
                            WHERE id=:id
                            FOR UPDATE
                            """
                        ),
                        {"id": attempt_id},
                    )
                ).mappings().one_or_none()
                if attempt is None:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                if int(attempt["chunk_id"]) != chunk_id:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                if int(attempt["generation"]) != expected_generation:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                chunk = (
                    await connection.execute(
                        text(
                            """
                            SELECT c.id, c.status, c.batch_id, c.route_generation,
                                   b.category
                            FROM sms_chunk c
                            JOIN sms_batch b ON b.id=c.batch_id
                            WHERE c.id=:id
                            FOR UPDATE OF c
                            """
                        ),
                        {"id": chunk_id},
                    )
                ).mappings().one_or_none()
                if chunk is None:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(chunk["batch_id"])},
                )
                if str(attempt["outcome"]) != "invoking":
                    return FinalizeReport(
                        classify_finalize_conflict(
                            attempt_outcome=str(attempt["outcome"]),
                            chunk_status=str(chunk["status"]),
                            requested=result,
                        ),
                        result,
                    )
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_vendor_attempt
                            SET outcome=:outcome,
                                safe_to_failover=:safe_to_failover,
                                vendor_code=:vendor_code,
                                updated_at=now()
                            WHERE id=:id AND outcome='invoking'
                            RETURNING id
                            """
                        ),
                        {
                            "id": attempt_id,
                            "outcome": result,
                            "safe_to_failover": safe_to_failover,
                            "vendor_code": vendor_code,
                        },
                    )
                ).scalar_one_or_none()
                if updated is None:
                    current = (
                        await connection.execute(
                            text(
                                """
                                SELECT a.outcome, c.status
                                FROM sms_vendor_attempt a
                                JOIN sms_chunk c ON c.id=a.chunk_id
                                WHERE a.id=:id
                                """
                            ),
                            {"id": attempt_id},
                        )
                    ).mappings().one()
                    return FinalizeReport(
                        classify_finalize_conflict(
                            attempt_outcome=str(current["outcome"]),
                            chunk_status=str(current["status"]),
                            requested=result,
                        ),
                        result,
                    )
                if result == "submitted":
                    submitted = await connection.execute(
                        text(
                            """
                            UPDATE sms_chunk SET status='submitted',
                              vendor_task_id=:task_id, submitted_at=now(),
                              submitting_since=NULL, retry_not_before=NULL
                            WHERE id=:id AND status='submitting'
                            RETURNING id
                            """
                        ),
                        {"id": chunk_id, "task_id": task_pseudonym},
                    )
                    if submitted.scalar_one_or_none() is None:
                        raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                    await settle_live_test_attempt(connection, chunk_id, "confirmed")
                    await connection.execute(
                        text("UPDATE sms_message SET status='sent' WHERE chunk_id=:id"),
                        {"id": chunk_id},
                    )
                elif result == "uncertain":
                    transitioned = await connection.execute(
                        text(
                            "UPDATE sms_chunk SET status='uncertain',"
                            "uncertain_since=COALESCE(submitting_since,now()),"
                            "submitting_since=NULL,retry_not_before=NULL "
                            "WHERE id=:id AND status='submitting' RETURNING id"
                        ),
                        {"id": chunk_id},
                    )
                    if transitioned.scalar_one_or_none() is None:
                        raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                    await settle_live_test_attempt(connection, chunk_id, "uncertain")
                elif result in {"retry_scheduled", "delayed"}:
                    delay_s = int(retry_delay_s or 1)
                    if result == "retry_scheduled":
                        changed = await connection.execute(
                            text(
                                "UPDATE sms_chunk c SET status='retrying',"
                                "vendor_code=:code,retry_count=retry_count+1,"
                                "submitting_since=NULL,"
                                "retry_not_before=now()+make_interval(secs=>:delay_s) "
                                "FROM sms_batch b "
                                "WHERE c.id=:id AND c.status='submitting' "
                                "AND c.retry_count=:expected_retry_count "
                                "AND c.retry_count<5 AND b.id=c.batch_id "
                                "RETURNING b.category"
                            ),
                            {
                                "id": chunk_id,
                                "code": vendor_code,
                                "expected_retry_count": expected_retry_count,
                                "delay_s": delay_s,
                            },
                        )
                    else:
                        changed = await connection.execute(
                            text(
                                """
                                UPDATE sms_chunk c SET status='retrying',
                                  vendor_code=:code, retry_count=retry_count+1,
                                  submitting_since=NULL,
                                  retry_not_before=now()+make_interval(secs=>:delay_s)
                                FROM sms_batch b
                                WHERE c.id=:id AND c.status='submitting'
                                  AND b.id=c.batch_id AND c.retry_count<8
                                RETURNING b.category
                                """
                            ),
                            {"id": chunk_id, "code": vendor_code, "delay_s": delay_s},
                        )
                    category = changed.scalar_one_or_none()
                    if category is None:
                        raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                    await settle_live_test_attempt(connection, chunk_id, "released")
                    enqueue = (chunk_id, str(category), delay_s)
                elif result == "paused":
                    if balance_blocked:
                        transitioned = await connection.execute(
                            text(
                                "UPDATE sms_chunk SET status='retrying',vendor_code=999,"
                                "submitting_since=NULL,retry_not_before=now() "
                                "WHERE id=:id AND status='submitting' RETURNING id"
                            ),
                            {"id": chunk_id},
                        )
                        if transitioned.scalar_one_or_none() is None:
                            raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                        await settle_live_test_attempt(connection, chunk_id, "released")
                        await connection.execute(
                            text(
                                "UPDATE sms_batch SET status='balance_blocked' WHERE id=:id"
                            ),
                            {"id": int(batch_id or chunk["batch_id"])},
                        )
                    else:
                        transitioned = await connection.execute(
                            text(
                                "UPDATE sms_chunk SET status='retrying',vendor_code=:code,"
                                "submitting_since=NULL,retry_not_before=now() "
                                "WHERE id=:id AND status='submitting' RETURNING id"
                            ),
                            {"id": chunk_id, "code": vendor_code},
                        )
                        if transitioned.scalar_one_or_none() is None:
                            raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                        await settle_live_test_attempt(connection, chunk_id, "released")
                elif result in {"rejected", "failed"} and not safe_to_failover:
                    failed = await connection.execute(
                        text(
                            """
                            UPDATE sms_chunk SET status='failed',vendor_code=:code,
                              vendor_msg=:message,submitting_since=NULL,
                              retry_not_before=NULL
                            WHERE id=:id AND status='submitting'
                            RETURNING batch_id
                            """
                        ),
                        {
                            "id": chunk_id,
                            "code": vendor_code,
                            "message": result,
                        },
                    )
                    transitioned_batch_id = failed.scalar_one_or_none()
                    if transitioned_batch_id is None:
                        raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
                    await settle_live_test_attempt(connection, chunk_id, "released")
                    await self._finalize_failed_messages(
                        connection,
                        chunk_id,
                        int(transitioned_batch_id),
                    )
                report = FinalizeReport(FinalizeKind.APPLIED, result)
        except _FinalizeRollback as exc:
            report = exc.report
        finally:
            await engine.dispose()
        if enqueue is not None and report.kind is FinalizeKind.APPLIED:
            await self._enqueue_retry(
                enqueue[0],
                self.lane_for(enqueue[1]),
                enqueue[2],
            )
        return report

    async def complete_vendor_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> bool:
        """把 invoking 行 CAS 到终态；败者不得再次调用供应商。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = (
                    await connection.execute(
                        text(
                            """
                            UPDATE sms_vendor_attempt
                            SET outcome=:outcome,
                                safe_to_failover=:safe_to_failover,
                                vendor_code=:vendor_code,
                                updated_at=now()
                            WHERE id=:id AND outcome='invoking'
                            RETURNING id
                            """
                        ),
                        {
                            "id": attempt_id,
                            "outcome": outcome,
                            "safe_to_failover": safe_to_failover,
                            "vendor_code": vendor_code,
                        },
                    )
                ).scalar_one_or_none()
                return updated is not None
        finally:
            await engine.dispose()

    async def record_vendor_attempt(
        self,
        chunk_id: int,
        *,
        vendor_id: str,
        generation: int,
        outcome: str,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> None:
        """记录一次无 PII 的供应商副作用，供路由对账与指标聚合。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_vendor_attempt (
                          chunk_id, vendor_id, generation, outcome,
                          safe_to_failover, vendor_code
                        ) VALUES (
                          :chunk_id, :vendor_id, :generation, :outcome,
                          :safe_to_failover, :vendor_code
                        )
                        """
                    ),
                    {
                        "chunk_id": chunk_id,
                        "vendor_id": vendor_id,
                        "generation": generation,
                        "outcome": outcome,
                        "safe_to_failover": safe_to_failover,
                        "vendor_code": vendor_code,
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET
                          selected_vendor=:vendor_id,
                          route_generation=:generation
                        WHERE id=:chunk_id
                        """
                    ),
                    {
                        "chunk_id": chunk_id,
                        "vendor_id": vendor_id,
                        "generation": generation,
                    },
                )
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


SPLIT_GENERATION = 1


async def complete_vendor_split(
    connection: AsyncConnection,
    chunk: ChunkPayload,
) -> list[int]:
    """同一事务扩容、终结父分片并创建身份化子分片；容量不足则阻塞且不回呼厂商。"""

    await connection.execute(
        text("SELECT pg_advisory_xact_lock(43, :batch)"),
        {"batch": int(chunk.batch_id)},
    )
    existing = await _existing_split_children(connection, chunk.chunk_id)
    if existing:
        return existing
    parent_status = (
        await connection.execute(
            text("SELECT status FROM sms_chunk WHERE id=:id FOR UPDATE"),
            {"id": chunk.chunk_id},
        )
    ).scalar_one_or_none()
    if parent_status not in {"submitting", "split_capacity_blocked"}:
        return []
    app_limit = await _split_app_limit(connection, chunk.batch_id)
    expanded = False
    if app_limit is not None:
        from app.services.pipeline import InFlightLimitExceeded
        from app.services.send_inflight import (
            InFlightInvariantViolation,
            expand_in_flight_for_split,
        )

        try:
            expanded = await expand_in_flight_for_split(
                connection,
                batch_id=chunk.batch_id,
                delta=1,
                limit=app_limit,
            )
        except InFlightLimitExceeded:
            expanded = False
        except InFlightInvariantViolation as error:
            if "reservation missing" not in str(error):
                raise
            expanded = False
    if not expanded:
        await _mark_split_capacity_blocked(connection, chunk.chunk_id)
        return []
    return await _create_split_children(connection, chunk)


async def retry_capacity_blocked_splits(
    connection: AsyncConnection,
    *,
    limit: int = 100,
) -> int:
    """容量释放后重试同一 generation 的阻塞拆分；不得改走 process_chunk 回呼厂商。"""

    rows = (
        await connection.execute(
            text(
                """
                SELECT id, batch_id, trim(custom_id) AS custom_id, phone_count
                FROM sms_chunk
                WHERE status='split_capacity_blocked'
                ORDER BY id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    retried = 0
    for row in rows:
        if int(row["phone_count"]) < 2:
            continue
        children = await complete_vendor_split(
            connection,
            ChunkPayload(
                chunk_id=int(row["id"]),
                batch_id=int(row["batch_id"]),
                custom_id=str(row["custom_id"]),
                phones=tuple("1" * 11 for _ in range(int(row["phone_count"]))),
                content="",
                template_id="",
                sign_name="",
            ),
        )
        if children:
            retried += 1
    return retried


async def _existing_split_children(
    connection: AsyncConnection,
    parent_chunk_id: int,
) -> list[int]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT id
                FROM sms_chunk
                WHERE parent_chunk_id=:parent_id
                  AND split_generation=:generation
                ORDER BY child_ordinal
                """
            ),
            {"parent_id": parent_chunk_id, "generation": SPLIT_GENERATION},
        )
    ).scalars()
    return [int(item) for item in rows]


async def _split_app_limit(connection: AsyncConnection, batch_id: int) -> int | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT a.max_in_flight_chunks
                FROM sms_batch b
                JOIN app a ON a.id=b.app_id
                WHERE b.id=:batch_id
                """
            ),
            {"batch_id": batch_id},
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return max(1, int(row))


async def _mark_split_capacity_blocked(
    connection: AsyncConnection,
    chunk_id: int,
) -> None:
    blocked = await connection.execute(
        text(
            """
            UPDATE sms_chunk SET
              status='split_capacity_blocked',
              vendor_code=1006,
              vendor_msg='split capacity blocked',
              submitting_since=NULL,
              retry_not_before=NULL
            WHERE id=:id AND status IN ('submitting','split_capacity_blocked')
            RETURNING id
            """
        ),
        {"id": chunk_id},
    )
    if blocked.scalar_one_or_none() is not None:
        await settle_live_test_attempt(connection, chunk_id, "released")


async def _create_split_children(
    connection: AsyncConnection,
    chunk: ChunkPayload,
) -> list[int]:
    from app.services.send_inflight import InFlightInvariantViolation

    parent = await connection.execute(
        text(
            """
            UPDATE sms_chunk SET status='failed',vendor_code=1006,
              vendor_msg='split into smaller chunks',submitting_since=NULL,
              retry_not_before=NULL
            WHERE id=:id AND status IN ('submitting','split_capacity_blocked')
            RETURNING id
            """
        ),
        {"id": chunk.chunk_id},
    )
    if parent.scalar_one_or_none() is None:
        raise InFlightInvariantViolation("split", "parent cas missed")
    await settle_live_test_attempt(connection, chunk.chunk_id, "released")
    messages = list(
        (
            await connection.execute(
                text(
                    "SELECT id,created_at FROM sms_message "
                    "WHERE chunk_id=:id ORDER BY id,created_at"
                ),
                {"id": chunk.chunk_id},
            )
        ).mappings()
    )
    if len(messages) < 2:
        raise InFlightInvariantViolation("split", "parent message set too small")
    batch = (
        (
            await connection.execute(
                text(
                    """
                    SELECT segments, usage_reservation_id, app_id, category
                    FROM sms_batch WHERE id=:id
                    """
                ),
                {"id": chunk.batch_id},
            )
        )
        .mappings()
        .one()
    )
    next_no = int(
        (
            await connection.execute(
                text("SELECT COALESCE(max(chunk_no),0) FROM sms_chunk WHERE batch_id=:id"),
                {"id": chunk.batch_id},
            )
        ).scalar_one()
    ) + 1
    middle = (len(messages) + 1) // 2
    child_ids: list[int] = []
    for ordinal, group in enumerate((messages[:middle], messages[middle:]), start=1):
        custom_id = f"{chunk.custom_id[:24]}{next_no:08d}"
        inserted = await connection.execute(
            text(
                """
                INSERT INTO sms_chunk(
                  batch_id,chunk_no,custom_id,phone_count,
                  parent_chunk_id,split_generation,child_ordinal
                ) VALUES (
                  :batch,:number,:custom,:count,
                  :parent_id,:generation,:ordinal
                )
                ON CONFLICT (parent_chunk_id, split_generation, child_ordinal)
                WHERE parent_chunk_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "batch": chunk.batch_id,
                "number": next_no,
                "custom": custom_id,
                "count": len(group),
                "parent_id": chunk.chunk_id,
                "generation": SPLIT_GENERATION,
                "ordinal": ordinal,
            },
        )
        child_id = inserted.scalar_one_or_none()
        if child_id is None:
            raise InFlightInvariantViolation("split", "child identity conflict")
        child_id = int(child_id)
        await connection.execute(
            text("UPDATE sms_message SET chunk_id=:chunk WHERE id=:id AND created_at=:at"),
            [{"chunk": child_id, "id": item["id"], "at": item["created_at"]} for item in group],
        )
        await connection.execute(
            text(
                """
                INSERT INTO usage_chunk_allocation (
                  chunk_id, batch_id, reservation_id,
                  recipient_count, segment_count,
                  request_count, app_id
                ) VALUES (
                  :chunk_id, :batch_id, :reservation_id,
                  :recipients, :segments,
                  0, :app_id
                )
                ON CONFLICT (chunk_id) DO NOTHING
                """
            ),
            {
                "chunk_id": child_id,
                "batch_id": chunk.batch_id,
                "reservation_id": batch["usage_reservation_id"],
                "recipients": len(group),
                "segments": int(batch["segments"] or 0) * len(group),
                "app_id": batch["app_id"],
            },
        )
        child_ids.append(child_id)
        next_no += 1
    await SqlChunkStore._enqueue_chunk_ready(
        connection,
        child_ids,
        SqlChunkStore.lane_for(str(batch["category"])),
    )
    return child_ids
