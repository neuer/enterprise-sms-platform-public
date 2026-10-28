"""下发前黑名单复核。只剔除尚未调用厂商的号码，并按计费条部分释放。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import bind_connection_system_audit
from app.services.billing import calculate_quota_cost
from app.services.category import policy_for_category
from app.services.crypto import CryptoService
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.usage_ledger_projection import (
    _change_projections,
    _lock_projection_keys,
    _ProjectionChange,
)
from app.services.vendor_test_guard import VendorTestRecipientDenied

REASON = "BLACKLISTED_BEFORE_SEND"
_AUDIT_ACTOR = "dispatch-blacklist"
_AUDIT_ACTION = "dispatch_excluded"
_PRE_SEND = ("pending", "retrying", "submitting", "failover_pending")
_FROZEN = ("submitted", "uncertain")
_RELEASABLE = ("reserved", "committed")


class DispatchAction(StrEnum):
    SEND = "send"
    EMPTY = "empty"
    FROZEN = "frozen"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """本次授权可以使用的号码。generation 为空表示类别豁免，不再比较策略代际。"""

    eligible_phones: tuple[str, ...]
    excluded_count: int
    generation: int | None
    action: DispatchAction


def notice_blacklist_flag(app_id: object, blacklist_check: object) -> bool:
    """没有应用时 notice 默认检查。类别矩阵仍由 category.py 决定。"""

    if app_id is None or blacklist_check is None:
        return True
    return bool(blacklist_check)


def blacklist_required(category: str, app_id: object, blacklist_check: object) -> bool:
    policy = policy_for_category(
        category,
        {category},
        notice_blacklist=notice_blacklist_flag(app_id, blacklist_check),
    )
    return policy.blacklist_required


async def guard_chunk(worker: Any, chunk: Any, *, persist_rejection: bool = True) -> bool:
    """测试收件人守卫。与黑名单复核分开，避免发送文件继续变长。"""

    if worker.enforce_live_test_recipients and chunk.denied_recipient_count:
        if persist_rejection:
            await worker.store.reject_disallowed_recipient(
                chunk.chunk_id,
                chunk.denied_recipient_count,
            )
        return False
    if worker.recipient_guard is None:
        return True
    try:
        worker.recipient_guard.require_allowed(chunk.phones)
    except VendorTestRecipientDenied as error:
        if persist_rejection:
            await worker.store.reject_disallowed_recipient(
                chunk.chunk_id,
                error.denied_count,
            )
        return False
    return True


async def phones_for_invoke(store: object, chunk: Any) -> DispatchDecision:
    """令牌等待之后复核。代际变化时不把旧号码交给 gateway.send。"""

    method = getattr(store, "eligible_dispatch_phones", None)
    if method is None:
        phones = tuple(str(phone) for phone in chunk.phones)
        action = DispatchAction.SEND if phones else DispatchAction.EMPTY
        return DispatchDecision(phones, 0, None, action)
    last = DispatchDecision((), 0, None, DispatchAction.STALE)
    for _ in range(3):
        decision = await method(chunk)
        if not isinstance(decision, DispatchDecision):
            phones = tuple(str(phone) for phone in decision)
            action = DispatchAction.SEND if phones else DispatchAction.EMPTY
            return DispatchDecision(phones, 0, None, action)
        if decision.action is not DispatchAction.SEND or decision.generation is None:
            return decision
        if await _generation(store) == decision.generation:
            return decision
        last = decision
    return DispatchDecision((), last.excluded_count, last.generation, DispatchAction.STALE)


async def filter_chunk_phones(store: Any, chunk: Any) -> DispatchDecision:
    crypto = getattr(store, "crypto", None)
    if not isinstance(crypto, CryptoService):
        crypto = CryptoService.from_settings(store.settings)
    engine = store._engine()
    try:
        async with engine.begin() as connection:
            return await decide_dispatch(connection, crypto, chunk)
    finally:
        await engine.dispose()


async def lock_blacklist_subjects(connection: AsyncConnection, digests: list[str]) -> None:
    """按摘要排序锁定号码主体。不在厂商 HTTP 期间持有。"""

    unique = sorted({item.strip() for item in digests if item and item.strip()})
    if not unique:
        return
    await connection.execute(
        text(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(locked.item, 91))
            FROM (
              SELECT unnest(CAST(:digests AS text[])) AS item
              ORDER BY item
            ) locked
            """
        ),
        {"digests": unique},
    )


async def bump_blacklist_generation(connection: AsyncConnection) -> int:
    value = await connection.scalar(text("SELECT bump_blacklist_policy_generation()"))
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError("blacklist policy generation unavailable")
    return value


async def decide_dispatch(
    connection: AsyncConnection,
    crypto: CryptoService,
    chunk: Any,
) -> DispatchDecision:
    """锁定分片。已提交或结果未知的分片不剔除、不退款、不再发送。"""

    header = await connection.execute(
        text(
            """
            SELECT c.status, c.batch_id, trim(b.batch_no) AS batch_no, b.category,
              b.app_id, b.usage_reservation_id, a.blacklist_check
            FROM sms_chunk c
            JOIN sms_batch b ON b.id=c.batch_id
            LEFT JOIN app a ON a.id=b.app_id
            WHERE c.id=:chunk_id
            FOR UPDATE OF c
            """
        ),
        {"chunk_id": chunk.chunk_id},
    )
    row = header.mappings().one_or_none()
    if row is None:
        raise RuntimeError("dispatch chunk unavailable")
    status = str(row["status"])
    if status in _FROZEN or status not in _PRE_SEND:
        return DispatchDecision((), 0, None, DispatchAction.FROZEN)
    pending = await _pending_messages(connection, chunk.chunk_id)
    if not pending:
        await _fail_unchunked(connection, chunk.chunk_id, status)
        await _settle_batch(connection, int(row["batch_id"]))
        return DispatchDecision((), 0, None, DispatchAction.EMPTY)
    loaded = [_Loaded(item, _decrypt(crypto, item)) for item in pending]
    if not blacklist_required(str(row["category"]), row["app_id"], row["blacklist_check"]):
        return DispatchDecision(
            tuple(item.phone for item in loaded),
            0,
            None,
            DispatchAction.SEND,
        )
    digests = sorted(
        {
            digest
            for item in loaded
            for digest in (*crypto.hmac_candidates(item.phone).values(), item.hmac)
        }
    )
    await lock_blacklist_subjects(connection, digests)
    generation = await _read_generation(connection)
    hits = await connection.execute(
        text(
            """
            SELECT m.id
            FROM sms_message m
            WHERE m.chunk_id=:chunk_id AND m.status='pending'
              AND EXISTS (
                SELECT 1 FROM blacklist_hmac_alias ba
                WHERE ba.hmac_digest=m.phone_hmac
              )
            """
        ),
        {"chunk_id": chunk.chunk_id},
    )
    hit_ids = {int(value) for value in hits.scalars()}
    excluded = 0
    released = False
    for item in loaded:
        if item.message_id not in hit_ids:
            continue
        if await _exclude_one(connection, item, row, generation):
            excluded += 1
            released = (
                await _release_one(
                    connection,
                    reservation_id=row["usage_reservation_id"],
                    phone_hmac=item.hmac,
                    segments=calculate_quota_cost(
                        f"{chunk.sign_name}{chunk.content}",
                        recipient_count=1,
                    ),
                )
                or released
            )
    eligible = tuple(item.phone for item in loaded if item.message_id not in hit_ids)
    if excluded:
        await _audit_exclusion(connection, str(row["batch_no"]), excluded)
        await connection.execute(
            text(
                """
                UPDATE usage_chunk_allocation
                SET recipient_count=GREATEST(recipient_count-:count,0),
                  segment_count=GREATEST(segment_count-:segments,0)
                WHERE chunk_id=:chunk_id
                """
            ),
            {
                "chunk_id": chunk.chunk_id,
                "count": excluded,
                "segments": calculate_quota_cost(
                    f"{chunk.sign_name}{chunk.content}",
                    recipient_count=excluded,
                ),
            },
        )
        if released and row["usage_reservation_id"] is not None:
            reservation_id = str(row["usage_reservation_id"])
            await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="usage.projection",
                    aggregate_type="usage_reservation",
                    aggregate_id=reservation_id,
                    task_name="app.tasks.outbox.publish_usage_projection",
                    queue="realtime",
                    args=(reservation_id,),
                    dedup_key=(
                        f"usage.projection:{reservation_id}:{generation}:{int(chunk.chunk_id)}"
                    ),
                ),
            )
        await _settle_batch(connection, int(row["batch_id"]))
    if not eligible:
        await _fail_unchunked(connection, chunk.chunk_id, status)
        await _settle_batch(connection, int(row["batch_id"]))
        return DispatchDecision((), excluded, generation, DispatchAction.EMPTY)
    return DispatchDecision(eligible, excluded, generation, DispatchAction.SEND)


class _Loaded:
    def __init__(self, row: RowMapping, phone: str) -> None:
        self.message_id = int(row["id"])
        self.created_at = row["created_at"]
        self.hmac = str(row["phone_hmac"])
        self.phone = phone


def _decrypt(crypto: CryptoService, row: RowMapping) -> str:
    phone = crypto.decrypt_phone(
        bytes(row["phone_enc"]),
        int(row["key_version"]),
        str(row["phone_hmac"]),
    )
    if not isinstance(phone, str) or not phone:
        raise RuntimeError("dispatch phone unavailable")
    return phone


async def _pending_messages(connection: AsyncConnection, chunk_id: int) -> list[Any]:
    result = await connection.execute(
        text(
            """
            SELECT m.id, m.created_at, m.phone_enc, trim(m.phone_hmac) AS phone_hmac,
              m.key_version
            FROM sms_message m
            WHERE m.chunk_id=:chunk_id AND m.status='pending'
            ORDER BY m.id
            """
        ),
        {"chunk_id": chunk_id},
    )
    return list(result.mappings())


async def _read_generation(connection: AsyncConnection) -> int:
    value = await connection.scalar(
        text("SELECT generation FROM blacklist_policy_generation WHERE id=1")
    )
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError("blacklist policy generation unavailable")
    return value


async def _generation(store: Any) -> int:
    engine = store._engine()
    try:
        async with engine.connect() as connection:
            return await _read_generation(connection)
    finally:
        await engine.dispose()


async def _exclude_one(
    connection: AsyncConnection, item: _Loaded, row: RowMapping, generation: int
) -> bool:
    inserted = await connection.execute(
        text(
            """
            INSERT INTO sms_dispatch_exclusion(
              message_id, message_created_at, batch_id, reason, decision_generation
            ) VALUES (
              :message_id, :created_at, :batch_id, :reason, :generation
            )
            ON CONFLICT DO NOTHING
            RETURNING message_id
            """
        ),
        {
            "message_id": item.message_id,
            "created_at": item.created_at,
            "batch_id": int(row["batch_id"]),
            "reason": REASON,
            "generation": generation,
        },
    )
    if inserted.scalar_one_or_none() is None:
        return False
    updated = await connection.execute(
        text(
            """
            UPDATE sms_message SET status='other'
            WHERE id=:message_id AND created_at=:created_at AND status='pending'
            RETURNING id
            """
        ),
        {"message_id": item.message_id, "created_at": item.created_at},
    )
    return updated.scalar_one_or_none() is not None


async def _release_one(
    connection: AsyncConnection,
    *,
    reservation_id: object,
    phone_hmac: str,
    segments: int,
) -> bool:
    """只减少仍为 reserved/committed 的账本。不调用整批释放。"""

    if reservation_id is None or segments < 1:
        return False
    reservation = UUID(str(reservation_id))
    state = await connection.scalar(
        text("SELECT state FROM usage_reservation WHERE id=:id FOR UPDATE"),
        {"id": reservation},
    )
    if state not in _RELEASABLE:
        return False
    changes: list[_ProjectionChange] = []
    quota = await connection.execute(
        text(
            """
            SELECT projection_key, usage_date, expires_at, amount
            FROM usage_quota_entry
            WHERE reservation_id=:reservation_id
            FOR UPDATE
            """
        ),
        {"reservation_id": reservation},
    )
    for entry in quota.mappings():
        current = int(entry["amount"])
        applied = min(segments, current)
        if applied <= 0:
            continue
        await connection.execute(
            text(
                """
                UPDATE usage_quota_entry
                SET amount=amount-:applied
                WHERE reservation_id=:reservation_id AND projection_key=:projection_key
                """
            ),
            {
                "applied": applied,
                "reservation_id": reservation,
                "projection_key": entry["projection_key"],
            },
        )
        changes.append(
            _ProjectionChange(
                dimension_key=str(entry["projection_key"]),
                kind="quota",
                usage_date=entry["usage_date"],
                window_key=entry["usage_date"].strftime("%Y%m%d"),
                delta=-applied,
                expires_at=entry["expires_at"],
                reset_on_window_change=False,
            )
        )
    frequency = await connection.execute(
        text(
            """
            UPDATE usage_frequency_entry e
            SET counted=false
            FROM usage_frequency_alias a
            WHERE e.reservation_id=:reservation_id AND e.counted
              AND a.subject_id=e.subject_id AND a.phone_hmac=:phone_hmac
            RETURNING e.projection_key, e.usage_date, e.window_key, e.expires_at
            """
        ),
        {"reservation_id": reservation, "phone_hmac": phone_hmac},
    )
    for entry in frequency.mappings():
        changes.append(
            _ProjectionChange(
                dimension_key=str(entry["projection_key"]),
                kind="frequency",
                usage_date=entry["usage_date"],
                window_key=str(entry["window_key"]),
                delta=-1,
                expires_at=entry["expires_at"],
                reset_on_window_change=False,
            )
        )
    merged = _unique_changes(changes)
    if not merged:
        return False
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in merged if change.kind == "frequency"],
        namespace=43,
    )
    await _lock_projection_keys(
        connection,
        [change.dimension_key for change in merged if change.kind == "quota"],
        namespace=47,
    )
    await _change_projections(connection, merged)
    return True


def _unique_changes(changes: list[_ProjectionChange]) -> list[_ProjectionChange]:
    merged: dict[str, _ProjectionChange] = {}
    for change in changes:
        current = merged.get(change.dimension_key)
        if current is None:
            merged[change.dimension_key] = change
            continue
        merged[change.dimension_key] = _ProjectionChange(
            dimension_key=current.dimension_key,
            kind=current.kind,
            usage_date=current.usage_date,
            window_key=current.window_key,
            delta=current.delta + change.delta,
            expires_at=current.expires_at,
            reset_on_window_change=False,
        )
    return list(merged.values())


async def _audit_exclusion(connection: AsyncConnection, batch_no: str, count: int) -> None:
    await bind_connection_system_audit(
        connection,
        actor_name=_AUDIT_ACTOR,
        action=_AUDIT_ACTION,
    )
    await connection.execute(
        text(
            """
            INSERT INTO audit_log(
              actor, actor_subject_kind, role, action, object_type, object_id, after_val
            ) VALUES (
              :actor, 'system', 'system', :action, 'batch',
              CAST(:batch_no AS varchar),
              jsonb_build_object(
                'count', CAST(:count AS integer),
                'reason', CAST(:reason AS text),
                'batch_no', CAST(:batch_no AS varchar)
              )
            )
            """
        ),
        {
            "actor": _AUDIT_ACTOR,
            "action": _AUDIT_ACTION,
            "batch_no": batch_no,
            "count": count,
            "reason": REASON,
        },
    )


async def _fail_unchunked(connection: AsyncConnection, chunk_id: int, status: str) -> None:
    if status == "submitting":
        return
    await connection.execute(
        text(
            """
            UPDATE sms_chunk
            SET status='failed', vendor_msg=:reason, submitting_since=NULL,
              retry_not_before=NULL
            WHERE id=:id AND status=:status
            """
        ),
        {"id": chunk_id, "status": status, "reason": REASON},
    )


async def _settle_batch(connection: AsyncConnection, batch_id: int) -> None:
    updated = await connection.execute(
        text(
            """
            UPDATE sms_batch b SET
              delivered=s.delivered, failed=s.failed, unknown_cnt=s.unknown_cnt,
              active_message_count=s.active,
              active_message_count_token=gen_random_uuid(),
              status=CASE
                WHEN b.status='completed_unknown' THEN 'completed_unknown'
                WHEN s.message_count=0 THEN b.status
                WHEN s.active=0 AND b.status IN ('queued','sending') THEN 'completed'
                ELSE b.status
              END,
              updated_at=now()
            FROM (
              SELECT count(*) message_count,
                count(*) FILTER (WHERE status='delivered') delivered,
                count(*) FILTER (WHERE status='failed') failed,
                count(*) FILTER (WHERE status='unknown') unknown_cnt,
                count(*) FILTER (WHERE status IN ('pending','sent')) active
              FROM sms_message WHERE batch_id=:batch_id
            ) s
            WHERE b.id=:batch_id
            RETURNING b.status
            """
        ),
        {"batch_id": batch_id},
    )
    if str(updated.scalar_one()) != "completed":
        return
    from app.services.callback_batch_event import enqueue_batch_finished
    from app.services.send_inflight import request_inflight_release_for_batch

    await enqueue_batch_finished(connection, batch_id)
    await request_inflight_release_for_batch(
        connection,
        batch_id=batch_id,
        reason="batch-completed",
    )
