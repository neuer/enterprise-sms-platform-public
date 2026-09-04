"""发送在途分片预留：PostgreSQL 权威 reserved→batch_bound→materialized→released。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass(frozen=True, slots=True)
class InFlightReservation:
    id: int
    generation: int
    estimated_chunks: int


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
        raise RuntimeError("in-flight reservation batch pointer failed")


async def release_in_flight_reservation(
    connection: Any,
    *,
    reservation_id: int,
    generation: int,
    reason: str,
) -> bool:
    """按 reservation identity CAS 释放；重复调用返回 False 且不二次扣减。"""

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
            RETURNING app_id, reserved_chunks
            """
        ),
        {
            "id": reservation_id,
            "generation": generation,
            "reason": reason,
        },
    )
    row = released.mappings().one_or_none()
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
    return repaired
