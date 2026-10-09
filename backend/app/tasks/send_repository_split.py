"""厂商 1006 容量拒绝后的原子拆分子片与容量阻塞重试。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.vendor_test_budget import (
    settle_live_test_attempt,
)
from app.tasks.send import (
    ChunkPayload,
)

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
    if len(existing) == 2:
        return existing
    if existing:
        from app.services.send_inflight import InFlightInvariantViolation

        raise InFlightInvariantViolation("split", "incomplete children")
    parent_status = (
        await connection.execute(
            text("SELECT status FROM sms_chunk WHERE id=:id FOR UPDATE"),
            {"id": chunk.chunk_id},
        )
    ).scalar_one_or_none()
    if parent_status not in {"submitting", "split_capacity_blocked"}:
        return []
    # 与整批用量补偿、回执应用共享短事务串行点：advisory→chunk→batch。
    await connection.execute(
        text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
        {"id": chunk.batch_id},
    )
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


async def _settle_split_vendor_attempt(
    connection: AsyncConnection,
    chunk_id: int,
) -> None:
    """把父分片上仍 invoking 的 attempt CAS 为 rejected，避免恢复器标 inconsistent。"""

    await connection.execute(
        text(
            """
            UPDATE sms_vendor_attempt
            SET outcome='rejected', vendor_code=1006, updated_at=now()
            WHERE chunk_id=:chunk_id AND outcome='invoking'
            """
        ),
        {"chunk_id": chunk_id},
    )


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
        await _settle_split_vendor_attempt(connection, chunk_id)


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
    await _settle_split_vendor_attempt(connection, chunk.chunk_id)
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
    next_no = (
        int(
            (
                await connection.execute(
                    text("SELECT COALESCE(max(chunk_no),0) FROM sms_chunk WHERE batch_id=:id"),
                    {"id": chunk.batch_id},
                )
            ).scalar_one()
        )
        + 1
    )
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
    await connection.execute(
        text(
            """
            UPDATE usage_chunk_allocation
            SET recipient_count=0, segment_count=0
            WHERE chunk_id=:parent_id
            """
        ),
        {"parent_id": chunk.chunk_id},
    )
    from app.tasks.send_repository import SqlChunkStore

    await SqlChunkStore._enqueue_chunk_ready(
        connection,
        child_ids,
        SqlChunkStore.lane_for(str(batch["category"])),
    )
    return child_ids
