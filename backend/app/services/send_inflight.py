"""发送在途分片预留：PostgreSQL 权威 reserved→batch_bound→materialized→released。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

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
    released = await connection.execute(
        text(
            """
            UPDATE send_inflight_reservation
            SET state='released',
                released_at=now(),
                release_reason=:reason,
                generation=generation + 1
            WHERE id=:id
              AND generation=:generation
              AND state <> 'released'
              AND app_id=COALESCE(CAST(:app_id AS BIGINT), app_id)
            RETURNING app_id, reserved_chunks
            """
        ),
        {
            "id": reservation_id,
            "generation": generation,
            "reason": reason,
            "app_id": app_id,
        },
    )
    return await _debit_released_balance(connection, released.mappings().one_or_none())


async def release_unbound_acceptance_reservation(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    app_id: int | None = None,
) -> bool:
    """仅释放仍为 reserved 且未绑定批次的受理失败预留。"""

    released = await connection.execute(
        text(
            """
            UPDATE send_inflight_reservation
            SET state='released',
                released_at=now(),
                release_reason='acceptance-failed',
                generation=generation + 1
            WHERE id=:id
              AND generation=:generation
              AND state='reserved'
              AND batch_id IS NULL
              AND app_id=COALESCE(CAST(:app_id AS BIGINT), app_id)
            RETURNING app_id, reserved_chunks
            """
        ),
        {
            "id": reservation_id,
            "generation": generation,
            "app_id": app_id,
        },
    )
    return await _debit_released_balance(connection, released.mappings().one_or_none())


async def _debit_released_balance(connection: Any, row: Any) -> bool:
    if row is None:
        return False
    await connection.execute(
        text(
            """
            UPDATE send_inflight_balance
            SET reserved_chunks=reserved_chunks - :amount,
                updated_at=now()
            WHERE app_id=:app_id
              AND reserved_chunks >= :amount
            """
        ),
        {"app_id": int(row["app_id"]), "amount": int(row["reserved_chunks"])},
    )
    return True


async def request_inflight_release_for_batch(
    connection: Any,
    *,
    batch_id: int,
    reason: str,
) -> bool:
    """终态批次幂等释放仍活动的预留。"""

    row = (
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
    ).mappings().one_or_none()
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
        await connection.execute(
            text(
                """
                SELECT id, generation, app_id, reserved_chunks, state
                FROM send_inflight_reservation
                WHERE batch_id=:batch_id AND state IN ('batch_bound','materialized')
                FOR UPDATE
                """
            ),
            {"batch_id": batch_id},
        )
    ).mappings().one_or_none()
    if current is None:
        return
    if str(current["state"]) == "materialized":
        return
    estimated = int(current["reserved_chunks"])
    app_id = int(current["app_id"])
    reservation_id = int(current["id"])
    generation = int(current["generation"])
    if actual_chunks > estimated:
        delta = actual_chunks - estimated
        expanded = await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=reserved_chunks + :delta,
                    updated_at=now()
                WHERE app_id=:app_id
                  AND reserved_chunks + :delta <= :limit
                RETURNING reserved_chunks
                """
            ),
            {"app_id": app_id, "delta": delta, "limit": limit},
        )
        if expanded.scalar_one_or_none() is None:
            raise RuntimeError("in-flight materialize exceeds application limit")
    elif actual_chunks < estimated:
        delta = estimated - max(actual_chunks, 1)
        await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=reserved_chunks - :delta,
                    updated_at=now()
                WHERE app_id=:app_id AND reserved_chunks >= :delta
                """
            ),
            {"app_id": app_id, "delta": delta},
        )
    occupancy = max(actual_chunks, 1)
    await connection.execute(
        text(
            """
            UPDATE send_inflight_reservation
            SET state='materialized',
                reserved_chunks=:occupancy,
                materialized_chunks=:actual,
                materialized_at=now()
            WHERE id=:id AND generation=:generation AND state='batch_bound'
            """
        ),
        {
            "id": reservation_id,
            "generation": generation,
            "occupancy": occupancy,
            "actual": actual_chunks,
        },
    )


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
        ).mappings().one_or_none()
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
    if state in {"batch_bound", "materialized"} or (
        state == "released" and batch_id is not None
    ):
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
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def _load_bound_batch(
    connection: Any,
    *,
    reservation_id: int,
    batch_id: int | None,
) -> dict[str, Any] | None:
    row = (
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
    ).mappings().first()
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
        restored = await connection.execute(
            text(
                """
                UPDATE send_inflight_reservation
                SET state='batch_bound',
                    released_at=NULL,
                    release_reason=NULL,
                    generation=generation + 1
                WHERE id=:id
                  AND generation=:generation
                  AND state='released'
                  AND release_reason='acceptance-failed'
                  AND batch_id=:batch_id
                RETURNING reserved_chunks, app_id
                """
            ),
            {
                "id": int(row["id"]),
                "generation": int(row["generation"]),
                "batch_id": batch_id,
            },
        )
        restored_row = restored.mappings().one_or_none()
        if restored_row is None:
            continue
        credited = await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=reserved_chunks + :amount,
                    updated_at=now()
                WHERE app_id=:app_id
                RETURNING app_id
                """
            ),
            {
                "app_id": int(restored_row["app_id"]),
                "amount": int(restored_row["reserved_chunks"]),
            },
        )
        if credited.scalar_one_or_none() is None:
            raise RuntimeError("in-flight balance restore failed")
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
    return repaired
