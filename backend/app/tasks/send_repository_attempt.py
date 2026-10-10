"""发送分片的厂商尝试状态机：开始调用、结果落定与安全拒绝 / 终态失败的应用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.crypto import CryptoService
from app.services.report_projection import NO_REPORT_EVIDENCE
from app.services.vendor_test_budget import (
    SubmissionClaim,
    settle_live_test_attempt,
)
from app.settings import Settings
from app.tasks.send import (
    ChunkPayload,
    FinalizeKind,
    FinalizeReport,
    classify_finalize_conflict,
)
from app.vendor.codes import DELAYED_RETRY_EXHAUSTED, DELAYED_RETRY_LIMIT
from app.vendor.failover import (
    FIRST_INVOKE_CHUNK_STATES,
    ISOLATING_CHUNK_STATES,
    NextAction,
    next_action_after_safe_reject,
)
from app.vendor.identifiers import vendor_identifier_pseudonym
from app.vendor.routing import (
    VendorAttempt,
    VendorRecord,
)


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


class VendorAttemptStoreMixin:
    """SqlChunkStore 的厂商尝试步骤；PostgreSQL 是事实源，CAS 失败整体回滚。"""

    crypto: CryptoService
    settings: Settings
    registry: tuple[VendorRecord, ...]
    adapter_ids: frozenset[str]

    if TYPE_CHECKING:
        # 由 SqlChunkStore 提供；运行时不定义桩，方法解析仍走 SqlChunkStore 的实现。
        def _engine(self) -> Any: ...

        @staticmethod
        def lane_for(category: str) -> str: ...

        @staticmethod
        async def _enqueue_retry(chunk_id: int, lane: str, countdown: int) -> None: ...

        @staticmethod
        async def _enqueue_chunk_ready(
            connection: AsyncConnection,
            chunk_ids: list[int],
            lane: str,
        ) -> None: ...

        async def _payload(self, connection: AsyncConnection, chunk_id: int) -> ChunkPayload: ...

        async def _reserve_submission_budget(
            self,
            connection: AsyncConnection,
            chunk_id: int,
            segments: int,
            attempt_no: int,
        ) -> SubmissionClaim: ...

        async def _finalize_failed_messages(
            self,
            connection: AsyncConnection,
            chunk_id: int,
            batch_id: int,
        ) -> None: ...

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
                        int(row["vendor_code"]) if row["vendor_code"] is not None else None,
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
                chunk = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT c.id, c.status, c.batch_id
                            FROM sms_chunk c
                            WHERE c.id=:id
                            FOR UPDATE
                            """
                            ),
                            {"id": chunk_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if chunk is None:
                    raise RuntimeError("vendor attempt chunk missing")
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(chunk["batch_id"])},
                )
                if str(chunk["status"]) in ISOLATING_CHUNK_STATES:
                    raise RuntimeError("vendor attempt blocked by terminal chunk")
                if str(chunk["status"]) not in FIRST_INVOKE_CHUNK_STATES:
                    raise RuntimeError("vendor attempt not first-invoke")
                history = (
                    (
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
                    )
                    .mappings()
                    .all()
                )
                if any(
                    str(row["outcome"]) in {"submitted", "uncertain", "invoking", "inconsistent"}
                    for row in history
                ):
                    raise RuntimeError("vendor attempt already irreversible")
                next_generation = max((int(row["generation"]) for row in history), default=0) + 1
                inserted = (
                    (
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
                    )
                    .mappings()
                    .one()
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
                    (
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
                    )
                    .mappings()
                    .one_or_none()
                )
                if attempt is None:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                if int(attempt["chunk_id"]) != chunk_id:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                if int(attempt["generation"]) != expected_generation:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                chunk = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT c.id, c.status, c.batch_id, c.route_generation,
                                   c.retry_count, c.vendor_msg,
                                   c.next_vendor, c.route_policy_version,
                                   c.failover_from_attempt_id, b.category,
                                   b.route_policy_version AS batch_policy_version,
                                   b.status AS batch_status
                            FROM sms_chunk c
                            JOIN sms_batch b ON b.id=c.batch_id
                            WHERE c.id=:id
                            FOR UPDATE OF c
                            """
                            ),
                            {"id": chunk_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if chunk is None:
                    return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(chunk["batch_id"])},
                )
                if str(attempt["outcome"]) != "invoking":
                    return self._report_from_locked_state(
                        attempt=attempt,
                        chunk=chunk,
                        requested=result,
                    )
                exhausted = result == "delayed" and int(chunk["retry_count"]) >= DELAYED_RETRY_LIMIT
                if exhausted:
                    result = "failed"
                    safe_to_failover = False
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
                        (
                            await connection.execute(
                                text(
                                    """
                                SELECT a.outcome, a.generation, a.id,
                                       c.status, c.next_vendor, c.route_generation,
                                       c.route_policy_version, c.failover_from_attempt_id
                                FROM sms_vendor_attempt a
                                JOIN sms_chunk c ON c.id=a.chunk_id
                                WHERE a.id=:id
                                """
                                ),
                                {"id": attempt_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    return self._report_from_locked_state(
                        attempt=current,
                        chunk=current,
                        requested=result,
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
                        text(
                            "UPDATE sms_message SET status='sent' WHERE chunk_id=:id "
                            f"AND status='pending' AND {NO_REPORT_EVIDENCE}"
                        ),
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
                                  AND b.id=c.batch_id AND c.retry_count<:retry_limit
                                RETURNING b.category
                                """
                            ),
                            {
                                "id": chunk_id,
                                "code": vendor_code,
                                "delay_s": delay_s,
                                "retry_limit": DELAYED_RETRY_LIMIT,
                            },
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
                            text("UPDATE sms_batch SET status='balance_blocked' WHERE id=:id"),
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
                elif result in {"rejected", "failed"}:
                    persisted = await self._apply_reject_or_fail(
                        connection,
                        chunk=chunk,
                        attempt_id=attempt_id,
                        expected_generation=expected_generation,
                        result=result,
                        vendor_code=vendor_code,
                        safe_to_failover=safe_to_failover,
                        failure_reason=DELAYED_RETRY_EXHAUSTED if exhausted else None,
                    )
                    report = FinalizeReport(
                        FinalizeKind.APPLIED,
                        result,
                        next_action=persisted.action.value,
                        next_vendor=persisted.next_vendor,
                        route_generation=persisted.route_generation,
                        previous_attempt_id=persisted.previous_attempt_id,
                        route_policy_version=persisted.route_policy_version,
                        chunk_status=(
                            "failover_pending"
                            if persisted.action is NextAction.FAILOVER_PENDING
                            else "failed"
                        ),
                    )
                if result not in {"rejected", "failed"}:
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

    def _report_from_locked_state(
        self,
        *,
        attempt: Any,
        chunk: Any,
        requested: str,
    ) -> FinalizeReport:
        """按已落库组合分类，并带回权威 next action。"""

        chunk_status = str(chunk["status"])
        actual = str(attempt["outcome"])
        if (
            requested == "delayed"
            and actual == "failed"
            and chunk_status == "failed"
            and chunk.get("vendor_msg") == DELAYED_RETRY_EXHAUSTED
        ):
            requested = actual
        kind = classify_finalize_conflict(
            attempt_outcome=actual,
            chunk_status=chunk_status,
            requested=requested,
        )
        next_action = None
        next_vendor = None
        if chunk_status == "failed":
            next_action = NextAction.FAILED.value
        elif chunk_status == "failover_pending":
            next_action = NextAction.FAILOVER_PENDING.value
            next_vendor = (
                str(chunk["next_vendor"]) if chunk.get("next_vendor") is not None else None
            )
        elif chunk_status == "retrying":
            next_action = NextAction.RETRYING.value
        return FinalizeReport(
            kind,
            actual,
            next_action=next_action,
            next_vendor=next_vendor,
            route_generation=(
                int(chunk["route_generation"])
                if chunk.get("route_generation") is not None
                else None
            ),
            previous_attempt_id=(
                int(chunk["failover_from_attempt_id"])
                if chunk.get("failover_from_attempt_id") is not None
                else None
            ),
            route_policy_version=(
                int(chunk["route_policy_version"])
                if chunk.get("route_policy_version") is not None
                else None
            ),
            chunk_status=chunk_status,
        )

    async def _load_attempts_for_update(
        self,
        connection: AsyncConnection,
        chunk_id: int,
    ) -> tuple[VendorAttempt, ...]:
        rows = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, vendor_id, generation, outcome,
                           safe_to_failover, vendor_code
                    FROM sms_vendor_attempt
                    WHERE chunk_id=:chunk_id
                    ORDER BY generation
                    FOR UPDATE
                    """
                    ),
                    {"chunk_id": chunk_id},
                )
            )
            .mappings()
            .all()
        )
        return tuple(
            VendorAttempt(
                str(row["vendor_id"]),
                int(row["generation"]),
                str(row["outcome"]),
                bool(row["safe_to_failover"]),
                int(row["vendor_code"]) if row["vendor_code"] is not None else None,
            )
            for row in rows
        )

    async def _apply_reject_or_fail(
        self,
        connection: AsyncConnection,
        *,
        chunk: Any,
        attempt_id: int,
        expected_generation: int,
        result: str,
        vendor_code: int | None,
        safe_to_failover: bool,
        failure_reason: str | None = None,
    ) -> Any:
        """把 rejected 与下一动作写进同一事务；永不留下 rejected+submitting。"""

        from app.vendor.failover import PersistedNextAction

        chunk_id = int(chunk["id"])
        category = str(chunk["category"] or "notice")
        policy_version = max(
            1,
            int(chunk.get("batch_policy_version") or chunk.get("route_policy_version") or 1),
        )
        if result == "failed" or not safe_to_failover:
            persisted = PersistedNextAction(
                action=NextAction.FAILED,
                route_generation=expected_generation,
                previous_attempt_id=attempt_id,
                route_policy_version=policy_version,
                reason="hard_fail",
            )
        else:
            attempts = await self._load_attempts_for_update(connection, chunk_id)
            persisted = next_action_after_safe_reject(
                attempts=attempts,
                category=category,
                previous_attempt_id=attempt_id,
                records=self.registry,
                adapter_ids=self.adapter_ids,
                policy_version=policy_version,
            )
        if persisted.action is NextAction.FAILOVER_PENDING:
            updated = await connection.execute(
                text(
                    """
                    UPDATE sms_chunk SET
                      status='failover_pending',
                      selected_vendor=:next_vendor,
                      next_vendor=:next_vendor,
                      route_generation=:generation,
                      route_policy_version=:policy_version,
                      failover_from_attempt_id=:attempt_id,
                      vendor_code=:code,
                      vendor_msg='safe_rejected_failover_pending',
                      submitting_since=NULL,
                      retry_not_before=now()
                    WHERE id=:id AND status='submitting'
                    RETURNING id
                    """
                ),
                {
                    "id": chunk_id,
                    "next_vendor": persisted.next_vendor,
                    "generation": expected_generation,
                    "policy_version": persisted.route_policy_version,
                    "attempt_id": attempt_id,
                    "code": vendor_code,
                },
            )
            if updated.scalar_one_or_none() is None:
                raise _FinalizeRollback(FinalizeReport(FinalizeKind.LOST_CAS, result))
            await settle_live_test_attempt(connection, chunk_id, "released")
            await self._enqueue_chunk_ready(
                connection,
                [chunk_id],
                self.lane_for(category),
            )
            return persisted
        failed = await connection.execute(
            text(
                """
                UPDATE sms_chunk SET status='failed',vendor_code=:code,
                  vendor_msg=:message,submitting_since=NULL,
                  retry_not_before=NULL, next_vendor=NULL,
                  failover_from_attempt_id=NULL
                WHERE id=:id AND status='submitting'
                RETURNING batch_id
                """
            ),
            {
                "id": chunk_id,
                "code": vendor_code,
                "message": failure_reason or result,
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
        return persisted

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
