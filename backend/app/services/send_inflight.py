"""发送在途分片预留：PostgreSQL 权威 reserved→batch_bound→materialized→released。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

ACTIVE_INFLIGHT_STATES = frozenset({"reserved", "batch_bound", "materialized"})
OCCUPYING_CHUNK_STATES = frozenset(
    {
        "pending",
        "submitting",
        "retrying",
        "submitted",
        "uncertain",
        "split_capacity_blocked",
    }
)
InFlightDeltaOperation = Literal[
    "reserve",
    "release",
    "materialize",
    "split",
    "repair",
    "reconcile",
]

AcceptCommitKind = Literal[
    "UNBOUND",
    "BOUND_TO_EXPECTED_BATCH",
    "BOUND_TO_CONFLICTING_BATCH",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class InFlightReservation:
    id: int
    generation: int
    estimated_chunks: int


@dataclass(frozen=True, slots=True)
class AcceptCommitResolution:
    """COMMIT 边界解析结果；只携带内部标识，不含完整业务键。"""

    kind: AcceptCommitKind
    batch_no: str | None = None
    reservation_state: str | None = None


class InFlightInvariantViolation(RuntimeError):
    """明细与聚合未同时更新；当前事务必须回滚。"""

    def __init__(self, operation: str, detail: str = "") -> None:
        self.operation = operation
        super().__init__(detail or f"在途容量守恒失败: {operation}")


def conservation_error(exc: BaseException) -> bool:
    """识别延迟守恒触发器或双侧更新失败。"""

    text = str(exc)
    return "send_inflight balance" in text or isinstance(exc, InFlightInvariantViolation)


async def apply_inflight_delta(
    connection: Any,
    *,
    operation: InFlightDeltaOperation,
    app_id: int,
    delta: int,
    reservation_id: int | None = None,
    generation: int | None = None,
    expected_states: frozenset[str] | None = None,
    next_state: str | None = None,
    next_reserved_chunks: int | None = None,
    reason: str | None = None,
    materialized_chunks: int | None = None,
    estimated: int | None = None,
    limit: int | None = None,
    unbound_only: bool = False,
) -> InFlightReservation | bool:
    """统一容量转换：先锁 balance 再锁 reservation，两侧都必须命中。"""

    if operation == "reserve":
        return await _apply_reserve(
            connection,
            app_id=app_id,
            estimated=estimated if estimated is not None else delta,
            limit=limit,
        )
    return await _apply_reservation_delta(
        connection,
        operation=operation,
        app_id=app_id,
        delta=delta,
        reservation_id=reservation_id,
        generation=generation,
        expected_states=expected_states,
        next_state=next_state,
        next_reserved_chunks=next_reserved_chunks,
        reason=reason,
        materialized_chunks=materialized_chunks,
        limit=limit,
        unbound_only=unbound_only,
    )


async def reserve_in_flight_chunks(
    connection: Any,
    *,
    app_id: int,
    estimated: int,
    limit: int,
) -> InFlightReservation:
    """受理预留：创建明细并增加聚合。"""

    created = await apply_inflight_delta(
        connection,
        operation="reserve",
        app_id=app_id,
        delta=estimated,
        estimated=estimated,
        limit=limit,
    )
    if not isinstance(created, InFlightReservation):
        raise InFlightInvariantViolation("reserve")
    return created


async def mark_conservation_blocked(connection: Any, app_id: int) -> None:
    """在独立事务中标记应用失败关闭，供新发送拒绝。"""

    await connection.execute(
        text(
            """
            UPDATE send_inflight_balance
            SET conservation_blocked_at=COALESCE(conservation_blocked_at, now()),
                updated_at=now()
            WHERE app_id=:app_id
            """
        ),
        {"app_id": app_id},
    )


async def _lock_balance(
    connection: Any,
    *,
    app_id: int,
    operation: str,
    allow_create: bool,
) -> dict[str, Any]:
    if allow_create:
        await connection.execute(
            text(
                """
                INSERT INTO send_inflight_balance(app_id, reserved_chunks)
                VALUES (:app_id, 0)
                ON CONFLICT (app_id) DO NOTHING
                """
            ),
            {"app_id": app_id},
        )
    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT reserved_chunks, conservation_blocked_at
                FROM send_inflight_balance
                WHERE app_id=:app_id
                FOR UPDATE
                """
                ),
                {"app_id": app_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise InFlightInvariantViolation(operation, "balance missing")
    if row["conservation_blocked_at"] is not None and operation == "reserve":
        raise InFlightInvariantViolation(operation, "conservation blocked")
    return dict(row)


async def _adjust_locked_balance(
    connection: Any,
    *,
    app_id: int,
    stored: int,
    delta: int,
    operation: str,
    limit: int | None,
) -> int:
    if delta == 0:
        return stored
    nxt = stored + delta
    if nxt < 0:
        raise InFlightInvariantViolation(operation, "balance below release")
    if limit is not None and nxt > limit:
        from app.services.pipeline import InFlightLimitExceeded

        raise InFlightLimitExceeded("应用在途分片已达上限")
    updated = (
        await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=:next_value,
                    updated_at=now()
                WHERE app_id=:app_id
                  AND reserved_chunks=:stored
                RETURNING reserved_chunks
                """
            ),
            {"app_id": app_id, "next_value": nxt, "stored": stored},
        )
    ).scalar_one_or_none()
    if updated is None:
        raise InFlightInvariantViolation(operation, "balance update missed")
    return int(updated)


async def _apply_reserve(
    connection: Any,
    *,
    app_id: int,
    estimated: int,
    limit: int | None,
) -> InFlightReservation:
    if estimated < 1 or limit is None or limit < 1:
        raise ValueError("in-flight reservation bounds invalid")
    locked = await _lock_balance(
        connection,
        app_id=app_id,
        operation="reserve",
        allow_create=True,
    )
    await _adjust_locked_balance(
        connection,
        app_id=app_id,
        stored=int(locked["reserved_chunks"]),
        delta=estimated,
        operation="reserve",
        limit=limit,
    )
    created = (
        (
            await connection.execute(
                text(
                    """
                INSERT INTO send_inflight_reservation (
                  app_id, reserved_chunks, state, generation, expires_at
                ) VALUES (
                  :app_id, :estimated, 'reserved', 1,
                  now() + interval '15 minutes'
                )
                RETURNING id, generation, reserved_chunks
                """
                ),
                {"app_id": app_id, "estimated": estimated},
            )
        )
        .mappings()
        .one_or_none()
    )
    if created is None:
        raise InFlightInvariantViolation("reserve", "reservation insert missed")
    return InFlightReservation(
        int(created["id"]),
        int(created["generation"]),
        int(created["reserved_chunks"]),
    )


async def _apply_reservation_delta(
    connection: Any,
    *,
    operation: str,
    app_id: int,
    delta: int,
    reservation_id: int | None,
    generation: int | None,
    expected_states: frozenset[str] | None,
    next_state: str | None,
    next_reserved_chunks: int | None,
    reason: str | None,
    materialized_chunks: int | None,
    limit: int | None,
    unbound_only: bool,
) -> bool:
    if reservation_id is None or generation is None:
        raise InFlightInvariantViolation(operation, "reservation identity required")
    locked = await _lock_balance(
        connection,
        app_id=app_id,
        operation=operation,
        allow_create=operation in {"repair", "reconcile"},
    )
    current = (
        (
            await connection.execute(
                text(
                    """
                SELECT id, generation, app_id, state, batch_id, reserved_chunks
                FROM send_inflight_reservation
                WHERE id=:id
                FOR UPDATE
                """
                ),
                {"id": reservation_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        return False
    if int(current["app_id"]) != app_id:
        raise InFlightInvariantViolation(operation, "reservation app mismatch")
    if int(current["generation"]) != generation:
        return False
    state = str(current["state"])
    if expected_states is not None and state not in expected_states:
        return False
    if unbound_only and (state != "reserved" or current["batch_id"] is not None):
        return False
    if operation == "release" and state == "released":
        return False
    amount = delta
    if operation == "release":
        amount = -int(current["reserved_chunks"])
    occupancy = (
        int(current["reserved_chunks"]) if next_reserved_chunks is None else next_reserved_chunks
    )
    if occupancy < 1:
        raise InFlightInvariantViolation(operation, "reservation occupancy invalid")
    await _adjust_locked_balance(
        connection,
        app_id=app_id,
        stored=int(locked["reserved_chunks"]),
        delta=amount,
        operation=operation,
        limit=limit,
    )
    updated = (
        await connection.execute(
            text(
                """
                UPDATE send_inflight_reservation
                SET state=COALESCE(CAST(:next_state AS VARCHAR), state),
                    reserved_chunks=:occupancy,
                    materialized_chunks=COALESCE(
                      CAST(:materialized_chunks AS INTEGER),
                      materialized_chunks
                    ),
                    materialized_at=CASE
                      WHEN CAST(:next_state AS VARCHAR)='materialized' THEN now()
                      ELSE materialized_at
                    END,
                    released_at=CASE
                      WHEN CAST(:next_state AS VARCHAR)='released' THEN now()
                      WHEN CAST(:next_state AS VARCHAR) IS NOT NULL
                       AND CAST(:next_state AS VARCHAR) <> 'released'
                        THEN NULL
                      ELSE released_at
                    END,
                    release_reason=CASE
                      WHEN CAST(:next_state AS VARCHAR)='released' THEN :reason
                      WHEN CAST(:next_state AS VARCHAR) IS NOT NULL
                       AND CAST(:next_state AS VARCHAR) <> 'released'
                        THEN NULL
                      ELSE release_reason
                    END,
                    generation=CASE
                      WHEN CAST(:next_state AS VARCHAR)='released' THEN generation + 1
                      ELSE generation
                    END
                WHERE id=:id
                  AND generation=:generation
                RETURNING id
                """
            ),
            {
                "id": reservation_id,
                "generation": generation,
                "next_state": next_state,
                "occupancy": occupancy,
                "materialized_chunks": materialized_chunks,
                "reason": reason,
            },
        )
    ).scalar_one_or_none()
    if updated is None:
        raise InFlightInvariantViolation(operation, "reservation update missed")
    return True


async def bind_in_flight_reservation(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    batch_id: int,
    app_id: int,
) -> None:
    """在保存批次的同一事务中把预留绑定到 batch。"""

    bound = await connection.execute(
        text(
            """
            UPDATE send_inflight_reservation
            SET batch_id=:batch_id,
                state='batch_bound',
                bound_at=now()
            WHERE id=:id
              AND generation=:generation
              AND app_id=:app_id
              AND state='reserved'
              AND batch_id IS NULL
            RETURNING id
            """
        ),
        {
            "id": reservation_id,
            "generation": generation,
            "batch_id": batch_id,
            "app_id": app_id,
        },
    )
    if bound.scalar_one_or_none() is None:
        raise RuntimeError("in-flight reservation bind failed")
    linked = await connection.execute(
        text(
            """
            UPDATE sms_batch
            SET send_inflight_reservation_id=:id
            WHERE id=:batch_id
              AND send_inflight_reservation_id IS NULL
            RETURNING id
            """
        ),
        {"id": reservation_id, "batch_id": batch_id},
    )
    if linked.scalar_one_or_none() is None:
        already = await connection.execute(
            text(
                """
                SELECT id
                FROM sms_batch
                WHERE id=:batch_id
                  AND send_inflight_reservation_id=:id
                """
            ),
            {"id": reservation_id, "batch_id": batch_id},
        )
        if already.scalar_one_or_none() is None:
            raise RuntimeError("in-flight reservation batch pointer failed")


async def release_in_flight_reservation(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    reason: str,
    app_id: int | None = None,
) -> bool:
    """按 reservation identity CAS 释放；重复调用返回 False 且不二次扣减。"""

    if reason == "acceptance-failed":
        return await release_unbound_acceptance_reservation(
            connection,
            reservation_id=reservation_id,
            generation=generation,
            app_id=app_id,
        )
    resolved_app = await _require_reservation_app(
        connection,
        reservation_id=reservation_id,
        app_id=app_id,
    )
    if resolved_app is None:
        return False
    released = await apply_inflight_delta(
        connection,
        operation="release",
        app_id=resolved_app,
        delta=0,
        reservation_id=reservation_id,
        generation=generation,
        expected_states=ACTIVE_INFLIGHT_STATES,
        next_state="released",
        reason=reason,
    )
    return bool(released)


async def release_unbound_acceptance_reservation(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    app_id: int | None = None,
) -> bool:
    """仅释放仍为 reserved 且未绑定批次的受理失败预留。"""

    resolved_app = await _require_reservation_app(
        connection,
        reservation_id=reservation_id,
        app_id=app_id,
    )
    if resolved_app is None:
        return False
    released = await apply_inflight_delta(
        connection,
        operation="release",
        app_id=resolved_app,
        delta=0,
        reservation_id=reservation_id,
        generation=generation,
        expected_states=frozenset({"reserved"}),
        next_state="released",
        reason="acceptance-failed",
        unbound_only=True,
    )
    return bool(released)


async def _require_reservation_app(
    connection: Any,
    *,
    reservation_id: int,
    app_id: int | None,
) -> int | None:
    if app_id is not None:
        return int(app_id)
    peeked = (
        await connection.execute(
            text(
                """
                SELECT app_id
                FROM send_inflight_reservation
                WHERE id=:id
                """
            ),
            {"id": reservation_id},
        )
    ).scalar_one_or_none()
    return None if peeked is None else int(peeked)


async def request_inflight_release_for_batch(
    connection: Any,
    *,
    batch_id: int,
    reason: str,
) -> bool:
    """终态批次幂等释放仍活动的预留。"""

    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT id, generation
                FROM send_inflight_reservation
                WHERE batch_id=:batch_id AND state <> 'released'
                """
                ),
                {"batch_id": batch_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False
    return await release_in_flight_reservation(
        connection,
        reservation_id=int(row["id"]),
        generation=int(row["generation"]),
        reason=reason,
    )


async def materialize_in_flight_reservation(
    connection: Any,
    *,
    batch_id: int,
    actual_chunks: int,
    limit: int,
) -> None:
    """按实际分片校准预留；实际大于估算且超上限时不得无界扩张。"""

    if actual_chunks < 0:
        raise ValueError("actual chunk count invalid")
    current = (
        (
            await connection.execute(
                text(
                    """
                SELECT id, generation, app_id, reserved_chunks, state
                FROM send_inflight_reservation
                WHERE batch_id=:batch_id AND state IN ('batch_bound','materialized')
                """
                ),
                {"batch_id": batch_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        return
    if str(current["state"]) == "materialized":
        return
    estimated = int(current["reserved_chunks"])
    occupancy = max(actual_chunks, 1)
    await apply_inflight_delta(
        connection,
        operation="materialize",
        app_id=int(current["app_id"]),
        delta=occupancy - estimated,
        reservation_id=int(current["id"]),
        generation=int(current["generation"]),
        expected_states=frozenset({"batch_bound"}),
        next_state="materialized",
        next_reserved_chunks=occupancy,
        materialized_chunks=actual_chunks,
        limit=limit,
    )


async def expand_in_flight_for_split(
    connection: Any,
    *,
    batch_id: int,
    delta: int,
    limit: int,
) -> bool:
    """运行期拆分：与创建子分片同一事务把 reservation/balance 净增加 delta。"""

    if delta < 1:
        raise ValueError("split expansion must be positive")
    current = (
        (
            await connection.execute(
                text(
                    """
                SELECT id, generation, app_id, reserved_chunks, materialized_chunks, state
                FROM send_inflight_reservation
                WHERE batch_id=:batch_id AND state IN ('batch_bound','materialized')
                FOR UPDATE
                """
                ),
                {"batch_id": batch_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        raise InFlightInvariantViolation("split", "reservation missing")
    occupancy = int(current["reserved_chunks"]) + delta
    materialized = (
        int(current["materialized_chunks"] or current["reserved_chunks"]) + delta
    )
    changed = await apply_inflight_delta(
        connection,
        operation="split",
        app_id=int(current["app_id"]),
        delta=delta,
        reservation_id=int(current["id"]),
        generation=int(current["generation"]),
        expected_states=frozenset({"batch_bound", "materialized"}),
        next_reserved_chunks=occupancy,
        materialized_chunks=materialized,
        limit=limit,
    )
    return bool(changed)


async def reconcile_split_occupancy_drift(
    connection: Any,
    *,
    limit: int = 100,
) -> int:
    """把低估的 materialized 预留补到当前占用分片数；只扩不缩。"""

    rows = (
        await connection.execute(
            text(
                """
                SELECT r.id, r.generation, r.app_id, r.reserved_chunks, r.batch_id
                FROM send_inflight_reservation r
                WHERE r.state='materialized'
                  AND r.batch_id IS NOT NULL
                ORDER BY r.id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    repaired = 0
    for row in rows:
        occupying = (
            await connection.execute(
                text(
                    """
                    SELECT COUNT(*)::integer
                    FROM sms_chunk
                    WHERE batch_id=:batch_id
                      AND status = ANY (send_chunk_occupying_states())
                    """
                ),
                {"batch_id": int(row["batch_id"])},
            )
        ).scalar_one()
        gap = int(occupying) - int(row["reserved_chunks"])
        if gap < 1:
            continue
        await apply_inflight_delta(
            connection,
            operation="repair",
            app_id=int(row["app_id"]),
            delta=gap,
            reservation_id=int(row["id"]),
            generation=int(row["generation"]),
            expected_states=frozenset({"materialized"}),
            next_reserved_chunks=int(row["reserved_chunks"]) + gap,
            materialized_chunks=int(row["reserved_chunks"]) + gap,
        )
        repaired += 1
    return repaired


async def resolve_ambiguous_acceptance_commit(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    app_id: int,
    scope_kind: str,
    scope_id: str,
    biz_id: str,
    request_hash: str,
) -> AcceptCommitResolution:
    """按 reservation / 批次 / 指纹解析 COMMIT 结果，禁止猜测释放。"""

    try:
        current = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, generation, app_id, state, batch_id, release_reason
                    FROM send_inflight_reservation
                    WHERE id=:id
                    FOR UPDATE
                    """
                    ),
                    {"id": reservation_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    except (OperationalError, DBAPIError):
        return AcceptCommitResolution("UNKNOWN")
    if current is None:
        return AcceptCommitResolution("UNKNOWN")
    state = str(current["state"])
    if int(current["app_id"]) != app_id:
        return AcceptCommitResolution(
            "BOUND_TO_CONFLICTING_BATCH",
            reservation_state=state,
        )
    batch_id = current["batch_id"]
    if state == "reserved" and batch_id is None:
        recorded = await _lookup_idempotent_batch(
            connection,
            scope_kind=scope_kind,
            scope_id=scope_id,
            biz_id=biz_id,
        )
        if recorded is None:
            return AcceptCommitResolution("UNBOUND", reservation_state=state)
        if not _same_subject(
            recorded,
            app_id=app_id,
            biz_id=biz_id,
            request_hash=request_hash,
        ):
            return AcceptCommitResolution(
                "BOUND_TO_CONFLICTING_BATCH",
                batch_no=str(recorded["batch_no"]),
                reservation_state=state,
            )
        pointer = recorded.get("send_inflight_reservation_id")
        if pointer is not None and int(pointer) != reservation_id:
            return AcceptCommitResolution(
                "BOUND_TO_CONFLICTING_BATCH",
                batch_no=str(recorded["batch_no"]),
                reservation_state=state,
            )
        return AcceptCommitResolution(
            "BOUND_TO_EXPECTED_BATCH",
            batch_no=str(recorded["batch_no"]),
            reservation_state=state,
        )
    if state in {"batch_bound", "materialized"} or (state == "released" and batch_id is not None):
        if int(current["generation"]) != generation and state != "released":
            return AcceptCommitResolution(
                "BOUND_TO_CONFLICTING_BATCH",
                reservation_state=state,
            )
        bound = await _load_bound_batch(
            connection,
            reservation_id=reservation_id,
            batch_id=None if batch_id is None else int(batch_id),
        )
        if bound is None:
            return AcceptCommitResolution("UNKNOWN", reservation_state=state)
        if not _same_subject(
            bound,
            app_id=app_id,
            biz_id=biz_id,
            request_hash=request_hash,
        ):
            return AcceptCommitResolution(
                "BOUND_TO_CONFLICTING_BATCH",
                batch_no=str(bound["batch_no"]),
                reservation_state=state,
            )
        return AcceptCommitResolution(
            "BOUND_TO_EXPECTED_BATCH",
            batch_no=str(bound["batch_no"]),
            reservation_state=state,
        )
    if state == "released":
        return AcceptCommitResolution("UNBOUND", reservation_state=state)
    return AcceptCommitResolution("UNKNOWN", reservation_state=state)


def _same_subject(
    row: Any,
    *,
    app_id: int,
    biz_id: str,
    request_hash: str,
) -> bool:
    stored_app = row.get("app_id")
    stored_biz = row.get("biz_id")
    stored_hash = row.get("request_hash")
    if stored_app is not None and int(stored_app) != app_id:
        return False
    if stored_biz is not None and str(stored_biz) != biz_id:
        return False
    return stored_hash is not None and str(stored_hash) == request_hash


async def _lookup_idempotent_batch(
    connection: Any,
    *,
    scope_kind: str,
    scope_id: str,
    biz_id: str,
) -> dict[str, Any] | None:
    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT b.id, trim(b.batch_no) AS batch_no, b.app_id, b.biz_id,
                       b.send_inflight_reservation_id, i.request_hash
                FROM idempotency_record i
                JOIN sms_batch b ON b.id=i.batch_id
                WHERE i.scope_kind=:scope_kind
                  AND i.scope_id=:scope_id
                  AND i.biz_id=:biz_id
                """
                ),
                {
                    "scope_kind": scope_kind,
                    "scope_id": scope_id,
                    "biz_id": biz_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def _load_bound_batch(
    connection: Any,
    *,
    reservation_id: int,
    batch_id: int | None,
) -> dict[str, Any] | None:
    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT b.id, trim(b.batch_no) AS batch_no, b.app_id, b.biz_id,
                       b.send_inflight_reservation_id, i.request_hash
                FROM sms_batch b
                LEFT JOIN idempotency_record i ON i.batch_id=b.id
                WHERE (
                    CAST(:batch_id AS BIGINT) IS NOT NULL
                    AND b.id=CAST(:batch_id AS BIGINT)
                  )
                  OR b.send_inflight_reservation_id=:reservation_id
                """
                ),
                {
                    "batch_id": batch_id,
                    "reservation_id": reservation_id,
                },
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


async def repair_misreleased_bound_reservations(
    connection: Any,
    *,
    limit: int = 100,
) -> int:
    """恢复仍活跃批次上被 acceptance-failed 误释放的预留，且禁止双份占用。"""

    rows = (
        await connection.execute(
            text(
                """
                SELECT r.id, r.generation, r.app_id, r.batch_id, r.reserved_chunks
                FROM send_inflight_reservation r
                JOIN sms_batch b ON b.id=r.batch_id
                WHERE r.state='released'
                  AND r.release_reason='acceptance-failed'
                  AND r.batch_id IS NOT NULL
                  AND b.status NOT IN (
                    'completed','completed_unknown','cancelled','rejected','expired'
                  )
                ORDER BY r.id
                FOR UPDATE OF r
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    repaired = 0
    for row in rows:
        batch_id = int(row["batch_id"])
        replacement = (
            await connection.execute(
                text(
                    """
                    SELECT id
                    FROM send_inflight_reservation
                    WHERE batch_id=:batch_id
                      AND state IN ('reserved','batch_bound','materialized')
                    LIMIT 1
                    """
                ),
                {"batch_id": batch_id},
            )
        ).scalar_one_or_none()
        if replacement is not None:
            continue
        restored = await apply_inflight_delta(
            connection,
            operation="repair",
            app_id=int(row["app_id"]),
            delta=int(row["reserved_chunks"]),
            reservation_id=int(row["id"]),
            generation=int(row["generation"]),
            expected_states=frozenset({"released"}),
            next_state="batch_bound",
            next_reserved_chunks=int(row["reserved_chunks"]),
        )
        if restored:
            repaired += 1
    return repaired


async def bind_reserved_reservation_to_existing_batch(
    connection: Any,
    *,
    limit: int = 100,
) -> int:
    """把仍 reserved 但批次指针已指向它的行补绑到同一事务合同。"""

    rows = (
        await connection.execute(
            text(
                """
                SELECT r.id, r.generation, r.app_id, b.id AS batch_id
                FROM send_inflight_reservation r
                JOIN sms_batch b ON b.send_inflight_reservation_id=r.id
                WHERE r.state='reserved'
                  AND r.batch_id IS NULL
                ORDER BY r.id
                FOR UPDATE OF r
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    repaired = 0
    for row in rows:
        await bind_in_flight_reservation(
            connection,
            reservation_id=int(row["id"]),
            generation=int(row["generation"]),
            batch_id=int(row["batch_id"]),
            app_id=int(row["app_id"]),
        )
        repaired += 1
    return repaired


async def reconcile_in_flight_reservations(connection: Any, *, limit: int = 100) -> int:
    """释放过期未绑定预留，并回收已终态批次上的活动预留。"""

    orphans = (
        await connection.execute(
            text(
                """
                SELECT id, generation
                FROM send_inflight_reservation
                WHERE state='reserved'
                  AND batch_id IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at < now()
                ORDER BY id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    repaired = 0
    for row in orphans:
        if await release_in_flight_reservation(
            connection,
            reservation_id=int(row["id"]),
            generation=int(row["generation"]),
            reason="orphan-expired",
        ):
            repaired += 1
    terminals = (
        await connection.execute(
            text(
                """
                SELECT r.id, r.generation, b.status
                FROM send_inflight_reservation r
                JOIN sms_batch b ON b.id=r.batch_id
                WHERE r.state <> 'released'
                  AND b.status IN (
                    'completed','completed_unknown','cancelled','rejected','expired'
                  )
                ORDER BY r.id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings()
    reasons = {
        "completed": "batch-completed",
        "completed_unknown": "batch-completed-unknown",
        "cancelled": "batch-cancelled",
        "rejected": "batch-rejected",
        "expired": "batch-expired",
    }
    for row in terminals:
        if await release_in_flight_reservation(
            connection,
            reservation_id=int(row["id"]),
            generation=int(row["generation"]),
            reason=reasons[str(row["status"])],
        ):
            repaired += 1
    repaired += await bind_reserved_reservation_to_existing_batch(
        connection,
        limit=limit,
    )
    repaired += await repair_misreleased_bound_reservations(
        connection,
        limit=limit,
    )
    repaired += await reconcile_split_occupancy_drift(connection, limit=limit)
    from app.tasks.send_repository import retry_capacity_blocked_splits

    repaired += await retry_capacity_blocked_splits(connection, limit=limit)
    repaired += await reconcile_inflight_balance_conservation(
        connection,
        limit=limit,
    )
    return repaired


async def reconcile_inflight_balance_conservation(
    connection: Any,
    *,
    limit: int = 100,
) -> int:
    """按 app 把 balance 收敛到 SUM(active reservations)，禁止猜测释放。"""

    apps = (
        await connection.execute(
            text(
                """
                SELECT app_id
                FROM (
                  SELECT app_id FROM send_inflight_balance
                  UNION
                  SELECT app_id FROM send_inflight_reservation
                   WHERE state IN ('reserved','batch_bound','materialized')
                ) apps
                ORDER BY app_id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).scalars()
    repaired = 0
    for app_id in apps:
        outcome = (
            await connection.execute(
                text("SELECT reconcile_send_inflight_app(:app_id)"),
                {"app_id": int(app_id)},
            )
        ).scalar_one()
        if outcome == "repaired":
            repaired += 1
    return repaired
