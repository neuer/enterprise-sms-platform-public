"""安全拒绝后的供应商切换认领、权威下一步读取与历史 safe reject 修复。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.vendor_test_budget import (
    SubmissionClaimStatus,
    settle_live_test_attempt,
)
from app.tasks.send import (
    FinalizeReport,
)
from app.tasks.send_repository_attempt import VendorAttemptStoreMixin
from app.vendor.failover import (
    InvokeAuthorization,
    InvokeClaim,
    InvokeClaimKind,
    NextAction,
    already_handled_reason,
    claim_denied_reason,
    historical_safe_reject_repairable,
    next_action_after_safe_reject,
)
from app.vendor.routing import (
    VendorAttempt,
    validate_vendor_id,
)


class VendorFailoverMixin(VendorAttemptStoreMixin):
    """SqlChunkStore 的切换认领；uncertain / submitted 之后禁止任何自动切换。"""

    async def claim_next_vendor_invoke(
        self,
        chunk_id: int,
        *,
        expected_route_generation: int,
        previous_attempt_id: int,
        expected_next_vendor: str,
        expected_route_policy_version: int,
        segments: int = 1,
        enforce_live_test_budget: bool = False,
    ) -> InvokeClaim:
        """领取下一跳 HTTP 授权；只有 COMMIT 为 invoking 后才允许外呼。"""

        expected_next = validate_vendor_id(expected_next_vendor)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                if bool(getattr(self.settings, "vendor_live_test", False)):
                    payload = await self._payload(connection, chunk_id)
                    if payload.denied_recipient_count:
                        return InvokeClaim(InvokeClaimKind.DENIED, reason="recipient_denied")
                previous = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT id, chunk_id, vendor_id, generation, outcome,
                                   safe_to_failover, vendor_code
                            FROM sms_vendor_attempt
                            WHERE id=:id
                            FOR UPDATE
                            """
                            ),
                            {"id": previous_attempt_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                chunk = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT c.id, c.status, c.batch_id, c.route_generation,
                                   c.next_vendor, c.route_policy_version,
                                   c.retry_not_before, c.vendor_attempt_count,
                                   b.category, b.status AS batch_status
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
                    return InvokeClaim(InvokeClaimKind.DENIED, reason="chunk_missing")
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                    {"id": int(chunk["batch_id"])},
                )
                history = (
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
                previous_attempt = None
                if previous is not None:
                    previous_attempt = VendorAttempt(
                        str(previous["vendor_id"]),
                        int(previous["generation"]),
                        str(previous["outcome"]),
                        bool(previous["safe_to_failover"]),
                        int(previous["vendor_code"])
                        if previous["vendor_code"] is not None
                        else None,
                    )
                invoking = next(
                    (row for row in history if str(row["outcome"]) == "invoking"),
                    None,
                )
                handled = already_handled_reason(
                    chunk_status=str(chunk["status"]),
                    invoking_vendor=(str(invoking["vendor_id"]) if invoking is not None else None),
                    expected_next_vendor=expected_next,
                    invoking_generation=(
                        int(invoking["generation"]) if invoking is not None else None
                    ),
                    expected_route_generation=expected_route_generation,
                )
                if handled is not None and invoking is not None:
                    return InvokeClaim(
                        InvokeClaimKind.ALREADY_HANDLED,
                        authorization=InvokeAuthorization(
                            int(invoking["id"]),
                            int(invoking["generation"]),
                            str(invoking["vendor_id"]),
                            str(invoking["vendor_id"]),
                            expected_route_policy_version,
                        ),
                        reason=handled,
                    )
                denied = claim_denied_reason(
                    chunk_status=str(chunk["status"]),
                    batch_status=str(chunk["batch_status"]),
                    expected_route_generation=expected_route_generation,
                    actual_route_generation=int(chunk["route_generation"] or 0),
                    previous_attempt=previous_attempt,
                    previous_attempt_chunk_id=(
                        int(previous["chunk_id"]) if previous is not None else None
                    ),
                    chunk_id=chunk_id,
                    expected_next_vendor=expected_next,
                    persisted_next_vendor=(
                        str(chunk["next_vendor"]) if chunk["next_vendor"] is not None else None
                    ),
                    expected_route_policy_version=expected_route_policy_version,
                    actual_route_policy_version=int(chunk["route_policy_version"] or 0),
                    retry_due=True,
                    blocking_outcomes=frozenset(str(row["outcome"]) for row in history),
                    category=str(chunk["category"] or "notice"),
                    records=self.registry,
                    adapter_ids=self.adapter_ids,
                )
                if denied is not None:
                    return InvokeClaim(InvokeClaimKind.DENIED, reason=denied)
                retry_due = (
                    await connection.execute(
                        text(
                            """
                            SELECT (retry_not_before IS NULL OR retry_not_before<=now())
                            FROM sms_chunk WHERE id=:id
                            """
                        ),
                        {"id": chunk_id},
                    )
                ).scalar_one()
                if not bool(retry_due):
                    return InvokeClaim(InvokeClaimKind.DENIED, reason="retry_not_due")
                if enforce_live_test_budget:
                    budget = await self._reserve_submission_budget(
                        connection,
                        chunk_id,
                        segments,
                        int(chunk["vendor_attempt_count"]) + 1,
                    )
                    if budget.status is not SubmissionClaimStatus.CLAIMED:
                        return InvokeClaim(
                            InvokeClaimKind.DENIED, reason="daily_limit", reset_at=budget.reset_at
                        )
                next_generation = expected_route_generation + 1
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
                              :adapter_id, 'safe_failover', now()
                            )
                            RETURNING id, generation, vendor_id, outcome
                            """
                            ),
                            {
                                "chunk_id": chunk_id,
                                "vendor_id": expected_next,
                                "generation": next_generation,
                                "adapter_id": expected_next,
                            },
                        )
                    )
                    .mappings()
                    .one()
                )
                claimed = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET
                          status='submitting',
                          vendor_attempt_count=vendor_attempt_count+:budget_attempt,
                          selected_vendor=:vendor_id,
                          route_generation=:generation,
                          submitting_since=now(),
                          retry_not_before=NULL
                        WHERE id=:id AND status='failover_pending'
                          AND route_generation=:expected_generation
                          AND next_vendor=:vendor_id
                        RETURNING id
                        """
                    ),
                    {
                        "id": chunk_id,
                        "vendor_id": expected_next,
                        "generation": next_generation,
                        "expected_generation": expected_route_generation,
                        "budget_attempt": int(enforce_live_test_budget),
                    },
                )
                if claimed.scalar_one_or_none() is None:
                    raise RuntimeError("failover claim lost cas")
                return InvokeClaim(
                    InvokeClaimKind.AUTHORIZED,
                    authorization=InvokeAuthorization(
                        int(inserted["id"]),
                        int(inserted["generation"]),
                        str(inserted["vendor_id"]),
                        str(inserted["vendor_id"]),
                        expected_route_policy_version,
                    ),
                    reason="authorized",
                )
        except Exception:
            return InvokeClaim(InvokeClaimKind.DENIED, reason="claim_failed")
        finally:
            await engine.dispose()

    async def load_authoritative_next_action(
        self,
        chunk_id: int,
        *,
        attempt_id: int,
        expected_generation: int,
    ) -> FinalizeReport | None:
        """COMMIT 结果未知时按 attempt/chunk/generation 回读事实。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT a.id, a.generation, a.outcome, c.status,
                                   c.next_vendor, c.route_generation,
                                   c.route_policy_version, c.failover_from_attempt_id
                            FROM sms_vendor_attempt a
                            JOIN sms_chunk c ON c.id=a.chunk_id
                            WHERE a.id=:attempt_id AND a.chunk_id=:chunk_id
                              AND a.generation=:generation
                            """
                            ),
                            {
                                "attempt_id": attempt_id,
                                "chunk_id": chunk_id,
                                "generation": expected_generation,
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                return self._report_from_locked_state(
                    attempt=row,
                    chunk=row,
                    requested=str(row["outcome"]),
                )
        finally:
            await engine.dispose()

    async def repair_legacy_safe_reject_submitting(
        self,
        connection: AsyncConnection,
        *,
        limit: int = 100,
    ) -> int:
        """有界修复历史 submitting+safe rejected；无法证明则隔离不猜测。"""

        rows = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT a.id AS attempt_id, c.id, c.status, c.batch_id, b.category,
                           COALESCE(b.route_policy_version,1) route_policy_version
                    FROM sms_chunk c
                    JOIN sms_batch b ON b.id=c.batch_id
                    JOIN sms_vendor_attempt a ON a.chunk_id=c.id
                    WHERE c.status='submitting'
                      AND a.generation=(
                        SELECT max(generation) FROM sms_vendor_attempt
                        WHERE chunk_id=c.id
                      )
                      AND a.outcome='rejected'
                      AND a.safe_to_failover
                    ORDER BY c.id
                    LIMIT :limit
                    """
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )
        repaired = 0
        for row in rows:
            chunk_id = int(row["id"])
            last_id = int(row["attempt_id"])
            await connection.execute(
                text("SELECT id FROM sms_vendor_attempt WHERE id=:id FOR UPDATE"),
                {"id": last_id},
            )
            await connection.execute(
                text("SELECT id FROM sms_chunk WHERE id=:id FOR UPDATE"),
                {"id": chunk_id},
            )
            await connection.execute(
                text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
                {"id": int(row["batch_id"])},
            )
            attempts = await self._load_attempts_for_update(connection, chunk_id)
            if not historical_safe_reject_repairable(
                chunk_status="submitting",
                attempts=attempts,
            ):
                continue
            persisted = next_action_after_safe_reject(
                attempts=attempts,
                category=str(row["category"] or "notice"),
                previous_attempt_id=int(last_id),
                records=self.registry,
                adapter_ids=self.adapter_ids,
                policy_version=int(row["route_policy_version"] or 1),
            )
            if persisted.action is NextAction.FAILOVER_PENDING:
                await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET
                          status='failover_pending',
                          selected_vendor=:next_vendor,
                          next_vendor=:next_vendor,
                          route_generation=:generation,
                          route_policy_version=:policy_version,
                          failover_from_attempt_id=:attempt_id,
                          submitting_since=NULL,
                          retry_not_before=now(),
                          vendor_msg='repaired_legacy_safe_reject'
                        WHERE id=:id AND status='submitting'
                        """
                    ),
                    {
                        "id": chunk_id,
                        "next_vendor": persisted.next_vendor,
                        "generation": persisted.route_generation,
                        "policy_version": persisted.route_policy_version,
                        "attempt_id": last_id,
                    },
                )
                await self._enqueue_chunk_ready(
                    connection,
                    [chunk_id],
                    self.lane_for(str(row["category"] or "notice")),
                )
            else:
                failed = await connection.execute(
                    text(
                        """
                        UPDATE sms_chunk SET status='failed',
                          vendor_msg='repaired_legacy_safe_reject',
                          submitting_since=NULL, retry_not_before=NULL,
                          next_vendor=NULL, failover_from_attempt_id=NULL
                        WHERE id=:id AND status='submitting'
                        RETURNING batch_id
                        """
                    ),
                    {"id": chunk_id},
                )
                batch_id = failed.scalar_one_or_none()
                if batch_id is not None:
                    await settle_live_test_attempt(connection, chunk_id, "released")
                    await self._finalize_failed_messages(connection, chunk_id, int(batch_id))
            repaired += 1
        return repaired
