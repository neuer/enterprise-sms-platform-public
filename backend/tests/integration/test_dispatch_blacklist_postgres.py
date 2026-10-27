"""下发前黑名单走真实 SQL。不调用厂商，也不把 Redis 当唯一计数。"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.runtime_resources import bind_connection_system_audit
from app.services.crypto import CryptoService
from app.services.dispatch_blacklist import (
    DispatchAction,
    decide_dispatch,
    lock_blacklist_subjects,
)
from app.tasks.send import ChunkPayload
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _crypto() -> CryptoService:
    secret = base64.b64encode(b"k" * 32).decode()
    return CryptoService.from_secret_values(secret, secret)


def _chunk(chunk_id: int, *, category: str, status: str = "pending") -> ChunkPayload:
    return ChunkPayload(
        chunk_id=chunk_id,
        batch_id=1,
        custom_id="c" * 32,
        phones=(),
        content="通知",
        template_id="",
        sign_name="",
        category=category,
        status=status,
    )


async def _prepare(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _insert_app(connection: Any, nonce: str, *, blacklist_check: bool) -> int:
    return int(
        (
            await connection.execute(
                text(
                    """
                    INSERT INTO app(
                      name,dept,api_key_hash,api_key_prefix,created_by,blacklist_check
                    ) VALUES(
                      :name,'平台部',:api_key_hash,:prefix,'test',:blacklist_check
                    ) RETURNING id
                    """
                ),
                {
                    "name": f"dispatch-{nonce}",
                    "api_key_hash": (nonce + nonce)[:64],
                    "prefix": nonce[:8],
                    "blacklist_check": blacklist_check,
                },
            )
        ).scalar_one()
    )


async def _seed(
    connection: Any,
    *,
    app_id: int,
    category: str,
    status: str,
    phones: list[str],
    reservation: bool,
) -> tuple[int, int, str | None]:
    crypto = _crypto()
    batch_no = uuid4().hex
    protected = [crypto.protect_phone(phone) for phone in phones]
    reservation_id = uuid4() if reservation else None
    if reservation_id is not None:
        await connection.execute(
            text(
                """
                INSERT INTO usage_reservation(
                  id,request_key,app_id,dept,category,usage_date,state,quota_cost
                ) VALUES(
                  :id,:key,:app_id,'平台部',:category,DATE '2026-10-27','committed',:cost
                )
                """
            ),
            {
                "id": reservation_id,
                "key": f"acceptance:{reservation_id}",
                "app_id": app_id,
                "category": category,
                "cost": len(phones),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO usage_quota_entry(
                  reservation_id,dimension_kind,dimension_value,usage_date,
                  amount,projection_key,expires_at
                ) VALUES(
                  :id,'app',:app_id,DATE '2026-10-27',:amount,:projection_key,:expires
                )
                """
            ),
            {
                "id": reservation_id,
                "app_id": str(app_id),
                "amount": len(phones),
                "projection_key": f"quota:app:{reservation_id.hex}",
                "expires": datetime.now(UTC) + timedelta(days=1),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO usage_projection(
                  dimension_key,kind,usage_date,window_key,value,expires_at
                ) VALUES(
                  :key,'quota',DATE '2026-10-27','20261027',:amount,:expires
                )
                """
            ),
            {
                "key": f"quota:app:{reservation_id.hex}",
                "amount": len(phones),
                "expires": datetime.now(UTC) + timedelta(days=1),
            },
        )
    batch_id = int(
        (
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_batch(
                      batch_no,channel,app_id,dept,content,category,
                      display_content_enc,send_content_enc,status,total,
                      usage_reservation_id
                    ) VALUES(
                      :batch_no,'api',:app_id,'平台部','[encrypted]',:category,
                      :content,:content,'sending',:total,:reservation_id
                    ) RETURNING id
                    """
                ),
                {
                    "batch_no": batch_no,
                    "app_id": app_id,
                    "category": category,
                    "content": b"cipher-content",
                    "total": len(phones),
                    "reservation_id": reservation_id,
                },
            )
        ).scalar_one()
    )
    chunk_id = int(
        (
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_chunk(
                      batch_id,chunk_no,custom_id,phone_count,status
                    ) VALUES(:batch_id,1,:custom_id,:total,:status)
                    RETURNING id
                    """
                ),
                {
                    "batch_id": batch_id,
                    "custom_id": uuid4().hex,
                    "total": len(phones),
                    "status": status,
                },
            )
        ).scalar_one()
    )
    for item in protected:
        await connection.execute(
            text(
                """
                INSERT INTO sms_message(
                  batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,key_version,status
                ) VALUES(
                  :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,:key_version,'pending'
                )
                """
            ),
            {
                "batch_id": batch_id,
                "chunk_id": chunk_id,
                "phone_enc": item.phone_enc,
                "phone_hmac": item.phone_hmac,
                "phone_mask": item.phone_mask,
                "key_version": item.key_version,
            },
        )
    return batch_id, chunk_id, batch_no


async def _blacklist(connection: Any, phone: str) -> str:
    protected = _crypto().protect_phone(phone)
    await connection.execute(
        text(
            """
            INSERT INTO blacklist(
              phone_hmac,phone_enc,phone_mask,key_version,source,created_by
            ) VALUES(:hmac,:enc,:mask,:version,'manual','test')
            """
        ),
        {
            "hmac": protected.phone_hmac,
            "enc": protected.phone_enc,
            "mask": protected.phone_mask,
            "version": protected.key_version,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO blacklist_hmac_alias(
              blacklist_digest,hmac_key_version,hmac_digest
            ) VALUES(:hmac,:version,:hmac)
            """
        ),
        {"hmac": protected.phone_hmac, "version": int(protected.key_version) + 1},
    )
    return protected.phone_hmac


async def _cleanup(
    engine: Any,
    *,
    app_id: int | None,
    batch_ids: list[int],
    hmacs: list[str],
) -> None:
    async with engine.begin() as connection:
        if batch_ids:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE aggregate_id IN (
                      SELECT usage_reservation_id::text FROM sms_batch
                      WHERE id=ANY(:ids) AND usage_reservation_id IS NOT NULL
                    )
                    """
                ),
                {"ids": batch_ids},
            )
            await connection.execute(
                text("DELETE FROM sms_dispatch_exclusion WHERE batch_id=ANY(:ids)"),
                {"ids": batch_ids},
            )
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE object_id IN "
                    "(SELECT trim(batch_no) FROM sms_batch WHERE id=ANY(:ids))"
                ),
                {"ids": batch_ids},
            )
            await connection.execute(
                text("DELETE FROM sms_message WHERE batch_id=ANY(:ids)"),
                {"ids": batch_ids},
            )
            await connection.execute(
                text("DELETE FROM usage_chunk_allocation WHERE batch_id=ANY(:ids)"),
                {"ids": batch_ids},
            )
            await connection.execute(
                text("DELETE FROM sms_chunk WHERE batch_id=ANY(:ids)"),
                {"ids": batch_ids},
            )
            reservations = await connection.execute(
                text(
                    """
                    SELECT usage_reservation_id FROM sms_batch
                    WHERE id=ANY(:ids) AND usage_reservation_id IS NOT NULL
                    """
                ),
                {"ids": batch_ids},
            )
            reservation_ids = [value for value in reservations.scalars()]
            await connection.execute(
                text("DELETE FROM sms_batch WHERE id=ANY(:ids)"),
                {"ids": batch_ids},
            )
            if reservation_ids:
                await connection.execute(
                    text(
                        """
                        DELETE FROM usage_projection
                        WHERE dimension_key IN (
                          SELECT projection_key FROM usage_quota_entry
                          WHERE reservation_id=ANY(:ids)
                        )
                        """
                    ),
                    {"ids": reservation_ids},
                )
                await connection.execute(
                    text("DELETE FROM usage_quota_entry WHERE reservation_id=ANY(:ids)"),
                    {"ids": reservation_ids},
                )
                await connection.execute(
                    text("DELETE FROM usage_reservation WHERE id=ANY(:ids)"),
                    {"ids": reservation_ids},
                )
        if app_id is not None:
            await connection.execute(text("DELETE FROM app WHERE id=:id"), {"id": app_id})
        if hmacs:
            await connection.execute(
                text("DELETE FROM blacklist_hmac_alias WHERE blacklist_digest=ANY(:hmacs)"),
                {"hmacs": hmacs},
            )
            await connection.execute(
                text("DELETE FROM blacklist WHERE phone_hmac=ANY(:hmacs)"),
                {"hmacs": hmacs},
            )


@pytest.mark.asyncio
async def test_market_recheck_releases_only_the_unsent_number_once() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    app_id = None
    batch_ids: list[int] = []
    hmacs: list[str] = []
    try:
        await _prepare(engine)
        nonce = uuid4().hex
        async with engine.begin() as connection:
            app_id = await _insert_app(connection, nonce, blacklist_check=False)
            hmacs.append(await _blacklist(connection, "13800138001"))
            batch_id, chunk_id, batch_no = await _seed(
                connection,
                app_id=app_id,
                category="market",
                status="pending",
                phones=["13800138000", "13800138001"],
                reservation=True,
            )
            batch_ids.append(batch_id)
        async with engine.begin() as connection:
            first = await decide_dispatch(
                connection, _crypto(), _chunk(chunk_id, category="market")
            )
        assert first.action is DispatchAction.SEND
        assert first.excluded_count == 1
        assert len(first.eligible_phones) == 1
        assert first.eligible_phones[0] == "13800138000"
        async with engine.begin() as connection:
            amount = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT amount FROM usage_quota_entry e
                            JOIN sms_batch b ON b.usage_reservation_id=e.reservation_id
                            WHERE b.id=:batch_id
                            """
                        ),
                        {"batch_id": batch_id},
                    )
                ).scalar_one()
            )
            statuses = [
                str(value)
                for value in (
                    await connection.execute(
                        text("SELECT status FROM sms_message WHERE batch_id=:id ORDER BY id"),
                        {"id": batch_id},
                    )
                ).scalars()
            ]
            exclusions = int(
                (
                    await connection.execute(
                        text("SELECT count(*) FROM sms_dispatch_exclusion WHERE batch_id=:id"),
                        {"id": batch_id},
                    )
                ).scalar_one()
            )
            audit_count = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='dispatch_excluded' AND object_id=:batch_no
                            """
                        ),
                        {"batch_no": batch_no},
                    )
                ).scalar_one()
            )
        assert amount == 1
        assert statuses == ["pending", "other"]
        assert exclusions == 1
        assert audit_count == 1
        async with engine.begin() as connection:
            second = await decide_dispatch(
                connection, _crypto(), _chunk(chunk_id, category="market")
            )
            amount_again = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT amount FROM usage_quota_entry e
                            JOIN sms_batch b ON b.usage_reservation_id=e.reservation_id
                            WHERE b.id=:batch_id
                            """
                        ),
                        {"batch_id": batch_id},
                    )
                ).scalar_one()
            )
        assert second.excluded_count == 0
        assert second.eligible_phones == ("13800138000",)
        assert amount_again == 1
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, hmacs=hmacs)
        await engine.dispose()


@pytest.mark.asyncio
async def test_submitted_and_verify_do_not_release() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    app_id = None
    batch_ids: list[int] = []
    hmacs: list[str] = []
    try:
        await _prepare(engine)
        nonce = uuid4().hex
        async with engine.begin() as connection:
            app_id = await _insert_app(connection, nonce, blacklist_check=False)
            hmacs.append(await _blacklist(connection, "13800138009"))
            verify_batch, verify_chunk, _batch_no = await _seed(
                connection,
                app_id=app_id,
                category="verify",
                status="pending",
                phones=["13800138009"],
                reservation=True,
            )
            submitted_batch, submitted_chunk, _submitted_no = await _seed(
                connection,
                app_id=app_id,
                category="notice",
                status="submitted",
                phones=["13800138009"],
                reservation=True,
            )
            batch_ids.extend([verify_batch, submitted_batch])
        async with engine.begin() as connection:
            verify = await decide_dispatch(
                connection, _crypto(), _chunk(verify_chunk, category="verify")
            )
            frozen = await decide_dispatch(
                connection,
                _crypto(),
                _chunk(submitted_chunk, category="notice", status="submitted"),
            )
        assert verify.action is DispatchAction.SEND
        assert verify.excluded_count == 0
        assert verify.eligible_phones == ("13800138009",)
        assert frozen.action is DispatchAction.FROZEN
        assert frozen.eligible_phones == ()
        async with engine.begin() as connection:
            amounts = [
                int(value)
                for value in (
                    await connection.execute(
                        text(
                            """
                            SELECT amount FROM usage_quota_entry e
                            JOIN sms_batch b ON b.usage_reservation_id=e.reservation_id
                            WHERE b.id=ANY(:ids) ORDER BY b.id
                            """
                        ),
                        {"ids": batch_ids},
                    )
                ).scalars()
            ]
        assert amounts == [1, 1]
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, hmacs=hmacs)
        await engine.dispose()


@pytest.mark.asyncio
async def test_subject_lock_blocks_until_the_holder_commits() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    digest = "ab" * 32
    started, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with engine.begin() as connection:
            await lock_blacklist_subjects(connection, [digest])
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(TimeoutError):
            async with engine.begin() as connection:
                await asyncio.wait_for(lock_blacklist_subjects(connection, [digest]), 0.2)
    finally:
        release.set()
        await holder
        await engine.dispose()
