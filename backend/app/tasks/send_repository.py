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
from app.services.crypto import CryptoService, EncryptionContext
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
from app.tasks.send import ChunkPayload
from app.vendor.identifiers import vendor_identifier_pseudonym

LOGGER = logging.getLogger(__name__)


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
        critical = await self.redis.get(f"queue:paused:{lane}")
        if critical is not None:
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

    async def prepare_chunks(
        self,
        batch_no: str,
        batch_size: int,
    ) -> tuple[list[ChunkPayload], str]:
        if batch_size < 1:
            raise ValueError("vendor_batch_size must be positive")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                batch_result = await connection.execute(
                    text(
                        "SELECT id, category, status FROM sms_batch "
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
                elif str(batch["status"]) == "queued":
                    await connection.execute(
                        text(
                            "UPDATE sms_batch SET status='sending',updated_at=now() "
                            "WHERE id=:id AND status='queued'"
                        ),
                        {"id": batch_id},
                    )
                payloads = [await self._payload(connection, chunk_id) for chunk_id in chunk_ids]
                return payloads, self.lane_for(str(batch["category"]))
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
                  status=CASE WHEN s.active=0 THEN 'completed' ELSE b.status END,
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
                elif await connection.scalar(
                    text(
                        "SELECT 1 FROM sms_chunk WHERE id=:id AND status='submitting'"
                    ),
                    {"id": chunk_id},
                ):
                    await connection.execute(
                        text(
                            "UPDATE sms_chunk SET status='failed',vendor_code=:code,"
                            "vendor_msg='delayed retry exhausted',submitting_since=NULL "
                            "WHERE id=:id AND status='submitting'"
                        ),
                        {"id": chunk_id, "code": code},
                    )
                    await settle_live_test_attempt(connection, chunk_id, "released")
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
        await self.redis.set("queue:paused:realtime", str(code))
        await self.redis.set("queue:paused:bulk", str(code))

    async def split_once(self, chunk: ChunkPayload) -> list[ChunkPayload]:
        if len(chunk.phones) < 2:
            return []
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(43, :batch)"),
                    {"batch": int(chunk.batch_id)},
                )
                rows_result = await connection.execute(
                    text(
                        "SELECT id,created_at FROM sms_message "
                        "WHERE chunk_id=:id ORDER BY id,created_at"
                    ),
                    {"id": chunk.chunk_id},
                )
                rows = list(rows_result.mappings())
                parent = await connection.execute(
                    text(
                        "UPDATE sms_chunk SET status='failed',vendor_code=1006,"
                        "vendor_msg='split into smaller chunks',submitting_since=NULL,"
                        "retry_not_before=NULL "
                        "WHERE id=:id AND status='submitting' RETURNING id"
                    ),
                    {"id": chunk.chunk_id},
                )
                if parent.scalar_one_or_none() is None:
                    return []
                await settle_live_test_attempt(connection, chunk.chunk_id, "released")
                max_result = await connection.execute(
                    text("SELECT COALESCE(max(chunk_no),0) FROM sms_chunk WHERE batch_id=:id"),
                    {"id": chunk.batch_id},
                )
                next_no = int(max_result.scalar_one()) + 1
                children: list[ChunkPayload] = []
                middle = (len(rows) + 1) // 2
                for group in (rows[:middle], rows[middle:]):
                    custom_id = f"{chunk.custom_id[:24]}{next_no:08d}"
                    inserted = await connection.execute(
                        text(
                            """
                            INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count)
                            VALUES (:batch,:number,:custom,:count) RETURNING id
                            """
                        ),
                        {
                            "batch": chunk.batch_id,
                            "number": next_no,
                            "custom": custom_id,
                            "count": len(group),
                        },
                    )
                    child_id = int(inserted.scalar_one())
                    await connection.execute(
                        text(
                            "UPDATE sms_message SET chunk_id=:chunk WHERE id=:id AND created_at=:at"
                        ),
                        [
                            {"chunk": child_id, "id": item["id"], "at": item["created_at"]}
                            for item in group
                        ],
                    )
                    children.append(await self._payload(connection, child_id))
                    next_no += 1
                return children
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
