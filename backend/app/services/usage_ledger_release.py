"""用量释放：唯一释放事件、投影回补、孤儿恢复与预留说明。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.usage_ledger_common import (
    UsageProjectionUnavailable,
    UsageReservationConflict,
    _safe_event_id,
)
from app.services.usage_ledger_projection import (
    ProjectionRow,
    UsageProjectionMixin,
    _change_projections,
    _database_now,
    _lock_projection_keys,
    _projection_rows,
    _ProjectionChange,
)


def _release_change_should_apply(
    change: _ProjectionChange,
    *,
    existing_keys: set[str],
    observed: datetime,
) -> bool:
    """投影存在则扣减；过期频控且目标缺失则跳过，避免归并后打空。"""

    if change.dimension_key in existing_keys:
        return True
    if change.kind == "frequency" and change.expires_at <= observed:
        return False
    raise UsageReservationConflict("projection batch update conflict")


async def _collect_release_projection_changes(
    connection: AsyncConnection,
    reservation_id: UUID,
) -> list[_ProjectionChange]:
    """按当前明细汇总负向投影变更；调用方必须已持有预留行锁。"""

    entries = await connection.execute(
        text(
            """
            SELECT projection_key,kind,usage_date,window_key,
                   sum(amount)::bigint amount,
                   max(expires_at) expires_at
            FROM (
              SELECT projection_key,'quota'::text kind,usage_date,
                     to_char(usage_date,'YYYYMMDD') window_key,
                     amount,expires_at
              FROM usage_quota_entry WHERE reservation_id=:reservation_id
              UNION ALL
              SELECT projection_key,'frequency'::text kind,usage_date,
                     window_key,
                     CASE WHEN counted THEN 1 ELSE 0 END amount,expires_at
              FROM usage_frequency_entry WHERE reservation_id=:reservation_id
            ) facts
            GROUP BY projection_key,kind,usage_date,window_key
            """
        ),
        {"reservation_id": reservation_id},
    )
    changes: list[_ProjectionChange] = []
    for entry in entries.mappings():
        amount = int(entry["amount"])
        if amount <= 0:
            continue
        changes.append(
            _ProjectionChange(
                dimension_key=str(entry["projection_key"]),
                kind=str(entry["kind"]),
                usage_date=entry["usage_date"],
                window_key=str(entry["window_key"]),
                delta=-amount,
                expires_at=entry["expires_at"],
                reset_on_window_change=False,
            )
        )
    return changes


async def _apply_release_projection_changes(
    connection: AsyncConnection,
    reservation_id: UUID,
) -> tuple[ProjectionRow, ...]:
    """按首次读到的键排序加锁后重读明细再扣减。

    不得在已持有 source 锁后再锁 canonical，否则会与归并的按序锁形成死锁。
    重读后若键已被改写：目标存在则直接扣减，过期且缺失则跳过。
    """

    observed = await _database_now(connection)
    initial = await _collect_release_projection_changes(connection, reservation_id)
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in initial if change.kind == "frequency"],
        namespace=43,
    )
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in initial if change.kind == "quota"],
        namespace=47,
    )
    changes = await _collect_release_projection_changes(connection, reservation_id)
    existing = {
        row.dimension_key
        for row in await _projection_rows(
            connection,
            [change.dimension_key for change in changes],
        )
    }
    applicable = [
        change
        for change in changes
        if _release_change_should_apply(
            change,
            existing_keys=existing,
            observed=observed,
        )
    ]
    return await _change_projections(connection, applicable)


async def _request_release(
    connection: AsyncConnection,
    *,
    reservation_id: UUID,
    event_id: str,
) -> tuple[bool, tuple[ProjectionRow, ...]]:
    """在调用方事务内建立唯一释放事实并扣减数据库权威投影。"""

    event_id = _safe_event_id(event_id)
    selected = await connection.execute(
        text(
            """
            SELECT state,release_event_id FROM usage_reservation
            WHERE id=:reservation_id FOR UPDATE
            """
        ),
        {"reservation_id": reservation_id},
    )
    row = selected.mappings().one_or_none()
    if row is None:
        raise UsageReservationConflict("usage reservation unavailable")
    state = str(row["state"])
    persisted_event = str(row["release_event_id"]) if row["release_event_id"] is not None else None
    changed = False
    rows: tuple[ProjectionRow, ...] = ()
    if state in {"reserved", "committed", "uncertain"}:
        if persisted_event is not None and persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        await connection.execute(
            text(
                """
                UPDATE usage_reservation SET
                  state='release_requested',release_event_id=:event_id,
                  release_requested_at=now(),updated_at=now()
                WHERE id=:reservation_id
                """
            ),
            {"reservation_id": reservation_id, "event_id": event_id},
        )
        rows = await _apply_release_projection_changes(connection, reservation_id)
        changed = True
    elif state == "release_requested":
        if persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        key_result = await connection.execute(
            text(
                """
                SELECT projection_key FROM usage_quota_entry
                WHERE reservation_id=:reservation_id
                UNION
                SELECT projection_key FROM usage_frequency_entry
                WHERE reservation_id=:reservation_id AND counted
                """
            ),
            {"reservation_id": reservation_id},
        )
        rows = await _projection_rows(
            connection,
            [str(value) for value in key_result.scalars()],
        )
    elif state == "released":
        if persisted_event is not None and persisted_event != event_id:
            raise UsageReservationConflict("usage release event changed")
        return False, ()
    else:
        raise UsageReservationConflict("invalid usage reservation state")

    await enqueue_outbox(
        connection,
        OutboxEventSpec(
            event_type="usage.release",
            aggregate_type="usage_reservation",
            aggregate_id=str(reservation_id),
            task_name="app.tasks.outbox.release_usage",
            queue="realtime",
            args=(str(reservation_id),),
            dedup_key=f"usage.release:{reservation_id}",
        ),
    )
    return changed, rows


async def request_usage_release_for_batch(
    connection: AsyncConnection,
    *,
    batch_id: int,
    event_id: str,
) -> bool:
    """终态业务事务内按 batch 稳定引用建立释放事实。"""

    result = await connection.execute(
        text(
            """
            SELECT usage_reservation_id FROM sms_batch
            WHERE id=:batch_id FOR UPDATE
            """
        ),
        {"batch_id": batch_id},
    )
    reservation_id = result.scalar_one_or_none()
    if reservation_id is None:
        return False
    await _request_release(
        connection,
        reservation_id=UUID(str(reservation_id)),
        event_id=event_id,
    )
    return True


class UsageReleaseMixin(UsageProjectionMixin):
    """UsageLedgerService 的释放与恢复；重复消费不得二次回补。"""

    async def request_release(self, reservation_id: UUID, *, event_id: str) -> bool:
        engine = self._engine()
        async with engine.begin() as connection:
            changed, _ = await _request_release(
                connection,
                reservation_id=reservation_id,
                event_id=event_id,
            )
            return changed

    async def request_unlinked_release(
        self,
        reservation_id: UUID,
        *,
        event_id: str,
    ) -> bool:
        """仅补偿尚未绑定批次的受理预留；与批次提交按行锁串行。"""

        engine = self._engine()
        async with engine.begin() as connection:
            selected = await connection.execute(
                text(
                    """
                    SELECT state FROM usage_reservation
                    WHERE id=:reservation_id FOR UPDATE
                    """
                ),
                {"reservation_id": reservation_id},
            )
            row = selected.mappings().one_or_none()
            if row is None:
                raise UsageReservationConflict("usage reservation unavailable")
            # 单独语句获得锁后的新 READ COMMITTED 快照；避免等待并发 batch
            # 提交时，LEFT JOIN 沿用等待前快照而误判为未绑定。
            linked = await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM sms_batch "
                    "WHERE usage_reservation_id=:reservation_id)"
                ),
                {"reservation_id": reservation_id},
            )
            if linked:
                return False
            changed, _ = await _request_release(
                connection,
                reservation_id=reservation_id,
                event_id=event_id,
            )
            return changed

    async def apply_release(self, reservation_id: UUID) -> int:
        """Outbox effect：覆盖全部受影响绝对投影，成功后才标记 released。"""

        engine = self._engine()
        async with engine.connect() as connection:
            selected = await connection.execute(
                text(
                    """
                    SELECT state FROM usage_reservation
                    WHERE id=:reservation_id
                    """
                ),
                {"reservation_id": reservation_id},
            )
            state = selected.scalar_one_or_none()
            if state is None:
                raise UsageReservationConflict("usage reservation unavailable")
            key_result = await connection.execute(
                text(
                    """
                    SELECT projection_key FROM usage_quota_entry
                    WHERE reservation_id=:reservation_id
                    UNION
                    SELECT projection_key FROM usage_frequency_entry
                    WHERE reservation_id=:reservation_id AND counted
                    """
                ),
                {"reservation_id": reservation_id},
            )
            rows = await _projection_rows(
                connection,
                [str(value) for value in key_result.scalars()],
            )
        await self._apply_rows(rows)
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE usage_reservation SET
                      state='released',released_at=COALESCE(released_at,now()),
                      last_error=NULL,updated_at=now()
                    WHERE id=:reservation_id AND state='release_requested'
                    """
                ),
                {"reservation_id": reservation_id},
            )
            if result.rowcount == 0:
                current = await connection.scalar(
                    text("SELECT state FROM usage_reservation WHERE id=:reservation_id"),
                    {"reservation_id": reservation_id},
                )
                if current != "released":
                    raise UsageReservationConflict("usage release state conflict")
                return 0
        return 1

    async def recover_orphans(self, *, older_than_seconds: int = 600) -> int:
        """把崩溃遗留的受理预留转为持久释放事件。"""

        if older_than_seconds < 60:
            raise ValueError("usage orphan threshold too small")
        engine = self._engine()
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id FROM usage_reservation
                    WHERE state IN ('reserved','uncertain')
                      AND updated_at < now()-make_interval(secs=>:seconds)
                    ORDER BY updated_at LIMIT 500
                    """
                ),
                {"seconds": older_than_seconds},
            )
            reservation_ids = [UUID(str(value)) for value in result.scalars()]
            stuck_result = await connection.execute(
                text(
                    """
                    SELECT id FROM usage_reservation
                    WHERE state='release_requested'
                      AND updated_at < now()-make_interval(secs=>:seconds)
                    ORDER BY updated_at LIMIT 500
                    """
                ),
                {"seconds": older_than_seconds},
            )
            stuck_ids = [UUID(str(value)) for value in stuck_result.scalars()]
        recovered = 0
        for reservation_id in reservation_ids:
            try:
                # 只回收尚未绑定批次的预留：扫描后、拿到行锁前预留可能已被
                # 并发批次提交为 committed，无绑定检查的释放会造成配额双花。
                released = await self.request_unlinked_release(
                    reservation_id,
                    event_id=f"usage:{reservation_id}:orphan-recovery",
                )
            except UsageReservationConflict:
                # 单行竞态（并发终态迁移/事件已占用）不阻断整轮回收。
                continue
            if released:
                recovered += 1
        for reservation_id in stuck_ids:
            try:
                # usage.release 事件死信后 apply 永不再来（#350）：直接重驱
                # apply_release。它按 DB 事实覆盖绝对投影且仅在
                # release_requested 状态迁移，与迟到的 Outbox 消费幂等共存。
                recovered += await self.apply_release(reservation_id)
            except (UsageReservationConflict, UsageProjectionUnavailable):
                continue
        return recovered

    async def explain(
        self,
        *,
        reservation_id: UUID | None = None,
        batch_no: str | None = None,
    ) -> dict[str, Any]:
        """解释计数来源，只返回主体 UUID 和窗口，不返回 HMAC 或手机号。"""

        if (reservation_id is None) == (batch_no is None):
            raise ValueError("provide exactly one usage reservation reference")
        engine = self._engine()
        async with engine.connect() as connection:
            if reservation_id is None:
                value = await connection.scalar(
                    text(
                        """
                        SELECT usage_reservation_id FROM sms_batch
                        WHERE batch_no=:batch_no
                        """
                    ),
                    {"batch_no": batch_no},
                )
                if value is None:
                    raise UsageReservationConflict("usage reservation unavailable")
                reservation_id = UUID(str(value))
            reservation_result = await connection.execute(
                text(
                    """
                    SELECT id,request_key,app_id,dept,category,usage_date,state,
                           quota_cost,release_event_id,reserved_at,
                           committed_at,release_requested_at,released_at
                    FROM usage_reservation WHERE id=:id
                    """
                ),
                {"id": reservation_id},
            )
            reservation = reservation_result.mappings().one_or_none()
            if reservation is None:
                raise UsageReservationConflict("usage reservation unavailable")
            quota_result = await connection.execute(
                text(
                    """
                    SELECT dimension_kind,dimension_value,amount,usage_date
                    FROM usage_quota_entry WHERE reservation_id=:id
                    ORDER BY dimension_kind
                    """
                ),
                {"id": reservation_id},
            )
            frequency_result = await connection.execute(
                text(
                    """
                    SELECT e.subject_id,e.category,e.window_kind,e.window_key,
                           e.counted,e.usage_date
                    FROM usage_frequency_entry e
                    WHERE e.reservation_id=:id
                    ORDER BY e.subject_id,e.window_kind
                    """
                ),
                {"id": reservation_id},
            )
            linked_batch = await connection.scalar(
                text(
                    """
                    SELECT trim(batch_no) FROM sms_batch
                    WHERE usage_reservation_id=:id
                    """
                ),
                {"id": reservation_id},
            )
        return {
            "reservation_id": str(reservation_id),
            "batch_no": str(linked_batch) if linked_batch is not None else None,
            "app_id": int(reservation["app_id"]),
            "dept": str(reservation["dept"]),
            "category": str(reservation["category"]),
            "usage_date": reservation["usage_date"].isoformat(),
            "state": str(reservation["state"]),
            "quota_cost": int(reservation["quota_cost"]),
            "quota_dimensions": [
                {
                    "kind": str(row["dimension_kind"]),
                    "value": str(row["dimension_value"]),
                    "amount": int(row["amount"]),
                }
                for row in quota_result.mappings()
            ],
            "frequency_dimensions": [
                {
                    "subject_id": str(row["subject_id"]),
                    "category": str(row["category"]),
                    "window_kind": str(row["window_kind"]),
                    "window_key": str(row["window_key"]),
                    "counted": bool(row["counted"]),
                }
                for row in frequency_result.mappings()
            ],
            "release_event_id": (
                str(reservation["release_event_id"])
                if reservation["release_event_id"] is not None
                else None
            ),
        }
