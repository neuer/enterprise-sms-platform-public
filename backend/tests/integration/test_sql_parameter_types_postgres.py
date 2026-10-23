"""改期、回复加黑和敏感词审计在真实 asyncpg 与 sms_accept 下的参数类型回归。"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.auth.accounts import ApplicationPrincipal, SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from app.services.batch_query import BatchAccessScope
from app.services.crypto import CryptoService, EncryptionContext
from app.services.pipeline_repository import SqlPipelineStore
from app.services.reply_query import SqlReplyQueryRepository
from app.services.scheduling import SchedulingService, StateConflict
from app.services.scheduling_repository import SqlSchedulingRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

PRINCIPAL_KEY = bytes.fromhex("33" * 32)


def _crypto() -> CryptoService:
    secret = base64.b64encode(b"k" * 32).decode()
    ring = '{"active_version":1,"keys":{"1":"' + secret + '"}}'
    return CryptoService.from_secret_values(ring, ring)


@pytest.fixture
async def accept_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{password}'"))
        if database:
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept'))
        await connection.execute(
            text(
                """
                INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at)
                VALUES ('principal',:key,now())
                ON CONFLICT (key_kind) DO UPDATE
                  SET key_material=EXCLUDED.key_material, updated_at=now()
                """
            ),
            {"key": PRINCIPAL_KEY},
        )
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: PRINCIPAL_KEY if name == "audit_context_key" else None,
    )
    try:
        yield owner, owner_url.set(username="sms_accept", password=password)
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _settings(url: URL) -> Any:
    return cast(Any, SimpleNamespace(database_url=url))


async def _admin(
    owner: AsyncEngine,
    login: str,
    *,
    role: str = "admin",
    dept: str = "平台部",
) -> SecurityPrincipal:
    async with owner.begin() as connection:
        account_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO user_account(display_name,dept,role)
                        VALUES(:login,:dept,:role) RETURNING id
                        """
                    ),
                    {"login": login, "dept": dept, "role": role},
                )
            ).scalar_one()
        )
        provider_id = int(
            (
                await connection.execute(
                    text("SELECT id FROM auth_provider WHERE code='local'")
                )
            ).scalar_one()
        )
        identity_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO auth_identity(
                          account_id,provider_id,login_name,
                          normalized_login_name,external_subject
                        ) VALUES(
                          :account_id,:provider_id,:login,:login,:external_subject
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "login": login,
                        "external_subject": f"local:{login}",
                    },
                )
            ).scalar_one()
        )
    return SecurityPrincipal(account_id, identity_id, login, dept, role)  # type: ignore[arg-type]


def _content(crypto: CryptoService, batch_no: str, plain: str) -> tuple[bytes, bytes]:
    display = crypto.encrypt_bound_packed_text(
        plain,
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=batch_no,
        ),
    )
    send = crypto.encrypt_bound_packed_text(
        plain,
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id=batch_no,
        ),
    )
    return display, send


class _Quota:
    async def refund_once(self, **_values: object) -> object:
        return object()


class _Publisher:
    async def enqueue(self, _batch_no: str, _queue: str) -> None:
        return None


async def _scheduled_batch(
    owner: AsyncEngine,
    crypto: CryptoService,
    *,
    channel: str,
    with_approval: bool,
) -> tuple[str, int, datetime]:
    batch_no = uuid4().hex
    display, send = _content(crypto, batch_no, "定时通知")
    original = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    async with owner.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,category,channel,dept,content,
                          display_content_enc,send_content_enc,status,scheduled_at
                        ) VALUES(
                          :batch_no,'notice',:channel,'平台部','[encrypted]',
                          :display,:send,'scheduled',:scheduled_at
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "channel": channel,
                        "display": display,
                        "send": send,
                        "scheduled_at": original,
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO idempotency_record(
                  scope_kind,scope_id,biz_id,batch_id,expires_at
                ) VALUES('web','平台部',:biz_id,:batch_id,now()+interval '1 day')
                """
            ),
            {"biz_id": uuid4().hex[:16], "batch_id": batch_id},
        )
        if with_approval:
            await connection.execute(
                text(
                    """
                    INSERT INTO approval(
                      batch_id,applicant,dept,status,expires_at
                    ) VALUES(:batch_id,'applicant01','平台部','approved',now()+interval '1 day')
                    """
                ),
                {"batch_id": batch_id},
            )
    return batch_no, batch_id, original


@pytest.mark.asyncio
async def test_reschedule_writes_shanghai_audit_and_reopens_approval(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    principal = await _admin(owner, f"sched-{uuid4().hex[:8]}")
    batch_no, batch_id, _original = await _scheduled_batch(
        owner, crypto, channel="web", with_approval=True
    )
    scheduled_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=10)
    repository = SqlSchedulingRepository(_settings(accept_url))
    service = SchedulingService(repository, _Quota(), _Publisher(), approval_expire_hours=6)
    try:
        with correlation_scope(), audit_principal_scope(principal):
            await service.reschedule(
                batch_no, BatchAccessScope(all_departments=True), scheduled_at
            )
        async with owner.connect() as connection:
            batch = (
                await connection.execute(
                    text(
                        """
                        SELECT status, scheduled_at
                        FROM sms_batch WHERE id=:batch_id
                        """
                    ),
                    {"batch_id": batch_id},
                )
            ).mappings().one()
            audit = (
                await connection.execute(
                    text(
                        """
                        SELECT actor, after_val->>'scheduled_at' AS scheduled_at
                        FROM audit_log
                        WHERE action='batch_reschedule' AND object_id=:batch_no
                        """
                    ),
                    {"batch_no": batch_no},
                )
            ).mappings().one()
            expires_at = (
                await connection.execute(
                    text("SELECT expires_at FROM idempotency_record WHERE batch_id=:batch_id"),
                    {"batch_id": batch_id},
                )
            ).scalar_one()
            approval = (
                await connection.execute(
                    text(
                        """
                        SELECT status, approver, expires_at
                        FROM approval WHERE batch_id=:batch_id
                        """
                    ),
                    {"batch_id": batch_id},
                )
            ).mappings().one()
        assert batch["status"] == "pending_approval"
        assert abs(batch["scheduled_at"].timestamp() - scheduled_at.timestamp()) < 0.001
        audited = datetime.fromisoformat(str(audit["scheduled_at"]))
        assert audited.utcoffset() == timedelta(hours=8)
        assert abs(audited.timestamp() - scheduled_at.timestamp()) < 0.001
        assert str(audit["actor"]) == principal.login_name
        assert abs((expires_at - (scheduled_at + timedelta(days=7))).total_seconds()) < 1
        assert approval["status"] == "pending"
        assert approval["approver"] is None
        assert approval["expires_at"] > datetime.now(UTC)
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM approval WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(
                text("DELETE FROM idempotency_record WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": principal.account_id},
            )
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch_id})
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )


@pytest.mark.asyncio
async def test_api_reschedule_keeps_scheduled_without_reapproval(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    principal = await _admin(owner, f"api-sched-{uuid4().hex[:8]}")
    batch_no, batch_id, _original = await _scheduled_batch(
        owner, crypto, channel="api", with_approval=False
    )
    scheduled_at = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    service = SchedulingService(
        SqlSchedulingRepository(_settings(accept_url)), _Quota(), _Publisher()
    )
    try:
        with correlation_scope(), audit_principal_scope(principal):
            await service.reschedule(
                batch_no, BatchAccessScope(all_departments=True), scheduled_at
            )
        async with owner.connect() as connection:
            status = (
                await connection.execute(
                    text("SELECT status FROM sms_batch WHERE id=:id"), {"id": batch_id}
                )
            ).scalar_one()
        assert status == "scheduled"
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM idempotency_record WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": principal.account_id},
            )
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch_id})
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )


async def _wait_for_row_lock(owner: AsyncEngine) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        async with owner.connect() as connection:
            waiting = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM pg_stat_activity
                            WHERE state='active'
                              AND wait_event_type='Lock'
                              AND query ILIKE '%FOR UPDATE%'
                              AND query ILIKE '%sms_batch%'
                            """
                        )
                    )
                ).scalar_one()
            )
        if waiting >= 1:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("改期事务没有在行锁上等待")


@pytest.mark.asyncio
async def test_concurrent_expiry_conflicts_without_partial_reschedule(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    import asyncio

    owner, accept_url = accept_runtime
    crypto = _crypto()
    principal = await _admin(owner, f"expire-{uuid4().hex[:8]}")
    batch_no, batch_id, original = await _scheduled_batch(
        owner, crypto, channel="api", with_approval=False
    )
    scheduled_at = (datetime.now(UTC) + timedelta(days=4)).replace(microsecond=0)
    service = SchedulingService(
        SqlSchedulingRepository(_settings(accept_url)), _Quota(), _Publisher()
    )

    async def reschedule() -> None:
        with correlation_scope(), audit_principal_scope(principal):
            await service.reschedule(
                batch_no, BatchAccessScope(all_departments=True), scheduled_at
            )

    holder = await owner.connect()
    transaction = await holder.begin()
    try:
        await holder.execute(
            text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"), {"id": batch_id}
        )
        task = asyncio.create_task(reschedule())
        await _wait_for_row_lock(owner)
        await holder.execute(
            text("UPDATE sms_batch SET status='expired', updated_at=now() WHERE id=:id"),
            {"id": batch_id},
        )
        await transaction.commit()
        with pytest.raises(StateConflict, match="仅 scheduled"):
            await task
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status, scheduled_at FROM sms_batch WHERE id=:id"),
                    {"id": batch_id},
                )
            ).mappings().one()
            audits = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='batch_reschedule' AND object_id=:batch_no
                            """
                        ),
                        {"batch_no": batch_no},
                    )
                ).scalar_one()
            )
        assert row["status"] == "expired"
        assert abs(row["scheduled_at"].timestamp() - original.timestamp()) < 0.001
        assert audits == 0
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await holder.close()
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM idempotency_record WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": principal.account_id},
            )
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch_id})
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )


@pytest.mark.asyncio
async def test_reschedule_audit_failure_rolls_back_the_batch(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    principal = await _admin(owner, f"sched-fail-{uuid4().hex[:8]}")
    batch_no, batch_id, original = await _scheduled_batch(
        owner, crypto, channel="web", with_approval=True
    )
    service = SchedulingService(
        SqlSchedulingRepository(_settings(accept_url)), _Quota(), _Publisher()
    )
    async with owner.begin() as connection:
        await connection.execute(text("REVOKE INSERT ON audit_log FROM sms_accept"))
    try:
        with (
            correlation_scope(),
            audit_principal_scope(principal),
            pytest.raises(DBAPIError),
        ):
            await service.reschedule(
                batch_no,
                BatchAccessScope(all_departments=True),
                (datetime.now(UTC) + timedelta(days=6)).replace(microsecond=0),
            )
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status, scheduled_at FROM sms_batch WHERE id=:id"),
                    {"id": batch_id},
                )
            ).mappings().one()
            approval = (
                await connection.execute(
                    text("SELECT status FROM approval WHERE batch_id=:id"), {"id": batch_id}
                )
            ).scalar_one()
            audits = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='batch_reschedule' AND object_id=:batch_no
                            """
                        ),
                        {"batch_no": batch_no},
                    )
                ).scalar_one()
            )
        assert row["status"] == "scheduled"
        assert abs(row["scheduled_at"].timestamp() - original.timestamp()) < 0.001
        assert approval == "approved"
        assert audits == 0
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("GRANT INSERT ON audit_log TO sms_accept"))
            await connection.execute(
                text("DELETE FROM approval WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(
                text("DELETE FROM idempotency_record WHERE batch_id=:id"), {"id": batch_id}
            )
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch_id})
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )


async def _reply(
    owner: AsyncEngine,
    crypto: CryptoService,
    *,
    phone: str,
    dept: str,
) -> tuple[int, str]:
    protected = crypto.protect_phone(phone, table="reply_event")
    batch_no = uuid4().hex
    event_key = uuid4().hex + uuid4().hex
    display, send = _content(crypto, batch_no, "上行所属批次")
    content = crypto.encrypt_bound_packed_text(
        "退订",
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=event_key,
        ),
    )
    async with owner.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,category,channel,dept,content,
                          display_content_enc,send_content_enc,status
                        ) VALUES(
                          :batch_no,'notice','web',:dept,'[encrypted]',
                          :display,:send,'completed'
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "dept": dept,
                        "display": display,
                        "send": send,
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO reply_event(
                  event_key,event_key_version,vendor_task_id,phone_enc,phone_hmac,
                  phone_mask,key_version,content,content_enc,is_optout,reply_time
                ) VALUES(
                  CAST(:event_key AS char(64)),1,CAST(:vendor_task_id AS varchar(64)),
                  :phone_enc,:phone_hmac,:phone_mask,:key_version,
                  '[encrypted]',:content_enc,TRUE,timestamptz '2026-07-15 09:00:00+08'
                )
                """
            ),
            {
                "event_key": event_key,
                "vendor_task_id": event_key,
                "phone_enc": protected.phone_enc,
                "phone_hmac": protected.phone_hmac,
                "phone_mask": protected.phone_mask,
                "key_version": protected.key_version,
                "content_enc": content,
            },
        )
        reply_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_reply(
                          event_key,batch_id,phone_enc,phone_hmac,phone_mask,key_version,
                          content,reply_time,created_at
                        ) VALUES(
                          :event_key,:batch_id,:phone_enc,:phone_hmac,:phone_mask,:key_version,
                          '[encrypted]',timestamptz '2026-07-15 09:00:00+08',
                          timestamptz '2026-07-15 09:00:00+08'
                        ) RETURNING id
                        """
                    ),
                    {
                        "event_key": event_key,
                        "batch_id": batch_id,
                        "phone_enc": protected.phone_enc,
                        "phone_hmac": protected.phone_hmac,
                        "phone_mask": protected.phone_mask,
                        "key_version": protected.key_version,
                    },
                )
            ).scalar_one()
        )
    return reply_id, protected.phone_hmac


async def _blacklist_count(owner: AsyncEngine, phone_hmac: str) -> int:
    async with owner.connect() as connection:
        return int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM blacklist WHERE trim(phone_hmac)=:hmac"),
                    {"hmac": phone_hmac},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_optout_admin_and_department_reencrypt_without_cross_department_write(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    admin = await _admin(owner, f"opt-admin-{uuid4().hex[:8]}")
    operator = await _admin(
        owner, f"opt-user-{uuid4().hex[:8]}", role="operator", dept="研发部"
    )
    admin_reply, admin_hmac = await _reply(owner, crypto, phone="13800138000", dept="研发部")
    dept_reply, dept_hmac = await _reply(owner, crypto, phone="13900139000", dept="研发部")
    repository = SqlReplyQueryRepository(_settings(accept_url), crypto)
    try:
        with correlation_scope():
            assert (
                await repository.optout(
                    admin_reply, dept="财务部", principal=operator, ip="10.0.0.8"
                )
                is False
            )
        assert await _blacklist_count(owner, admin_hmac) == 0
        async with owner.connect() as connection:
            audits = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='reply_optout' AND object_id=:reply_id
                            """
                        ),
                        {"reply_id": str(admin_reply)},
                    )
                ).scalar_one()
            )
        assert audits == 0
        with correlation_scope():
            assert (
                await repository.optout(admin_reply, dept=None, principal=admin, ip="10.0.0.8")
                is True
            )
            assert (
                await repository.optout(admin_reply, dept=None, principal=admin, ip="10.0.0.8")
                is True
            )
            assert (
                await repository.optout(
                    dept_reply, dept="研发部", principal=operator, ip="10.0.0.9"
                )
                is True
            )
        assert await _blacklist_count(owner, admin_hmac) == 1
        assert await _blacklist_count(owner, dept_hmac) == 1
        async with owner.connect() as connection:
            stored = (
                await connection.execute(
                    text(
                        """
                        SELECT phone_enc, phone_hmac, phone_mask, key_version
                        FROM blacklist WHERE trim(phone_hmac)=:hmac
                        """
                    ),
                    {"hmac": admin_hmac},
                )
            ).mappings().one()
            reply_enc = (
                await connection.execute(
                    text("SELECT phone_enc FROM sms_reply WHERE id=:id"),
                    {"id": admin_reply},
                )
            ).scalar_one()
        assert bytes(stored["phone_enc"]) != bytes(reply_enc)
        assert "13800138000" not in bytes(stored["phone_enc"]).decode("utf-8", "ignore")
        assert (
            crypto.decrypt_phone(
                bytes(stored["phone_enc"]),
                int(stored["key_version"]),
                str(stored["phone_hmac"]).strip(),
                table="blacklist",
            )
            == "13800138000"
        )
        assert str(stored["phone_mask"]) == "138****8000"
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("DELETE FROM blacklist_hmac_alias"))
            await connection.execute(text("DELETE FROM blacklist"))
            await connection.execute(text("DELETE FROM sms_reply"))
            await connection.execute(text("DELETE FROM reply_event"))
            await connection.execute(text("DELETE FROM audit_log WHERE action='reply_optout'"))
            await connection.execute(
                text("DELETE FROM sms_batch WHERE dept IN ('研发部','平台部')")
            )
            for principal in (admin, operator):
                await connection.execute(
                    text("DELETE FROM auth_identity WHERE id=:id"),
                    {"id": principal.identity_id},
                )
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=:id"),
                    {"id": principal.account_id},
                )


@pytest.mark.asyncio
async def test_optout_audit_failure_does_not_keep_a_blacklist_row(
    accept_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    admin = await _admin(owner, f"opt-fail-{uuid4().hex[:8]}")
    reply_id, phone_hmac = await _reply(owner, crypto, phone="13700137000", dept="研发部")
    repository = SqlReplyQueryRepository(_settings(accept_url), crypto)

    async def fail(_connection: object, _event: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr("app.services.blacklist_repository.insert_audit", fail)
    try:
        with correlation_scope(), pytest.raises(RuntimeError, match="synthetic"):
            await repository.optout(reply_id, dept=None, principal=admin, ip="10.0.0.8")
        assert await _blacklist_count(owner, phone_hmac) == 0
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("DELETE FROM blacklist_hmac_alias"))
            await connection.execute(text("DELETE FROM blacklist"))
            await connection.execute(text("DELETE FROM sms_reply"))
            await connection.execute(text("DELETE FROM reply_event"))
            await connection.execute(text("DELETE FROM sms_batch WHERE dept='研发部'"))
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": admin.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": admin.account_id}
            )


@pytest.mark.asyncio
async def test_sensitive_hit_audit_stores_integer_and_failure_leaves_no_row(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    store = SqlPipelineStore(_settings(accept_url))
    async with owner.begin() as connection:
        app_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,created_by
                        ) VALUES(
                          :name,'平台部',:api_key_hash,'12345678','integration'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"sens-{uuid4().hex[:12]}",
                        "api_key_hash": uuid4().hex + uuid4().hex,
                    },
                )
            ).scalar_one()
        )
    principal = ApplicationPrincipal(app_id, "sens-app", "平台部")
    async with owner.begin() as connection:
        await connection.execute(text("REVOKE INSERT ON audit_log FROM sms_accept"))
    try:
        with (
            correlation_scope(),
            audit_principal_scope(principal),
            pytest.raises(DBAPIError),
        ):
            await store.audit_sensitive_hit(principal, 4)
        async with owner.connect() as connection:
            failed = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='sensitive_hit' AND actor_app_id=:app_id
                            """
                        ),
                        {"app_id": app_id},
                    )
                ).scalar_one()
            )
        assert failed == 0
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("GRANT INSERT ON audit_log TO sms_accept"))
    try:
        with correlation_scope(), audit_principal_scope(principal):
            await store.audit_sensitive_hit(principal, 3)
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT jsonb_typeof(after_val->'hit_count') AS kind,
                          after_val
                        FROM audit_log
                        WHERE action='sensitive_hit' AND actor_app_id=:app_id
                        """
                    ),
                    {"app_id": app_id},
                )
            ).mappings().one()
        assert row["kind"] == "number"
        assert row["after_val"] == {"hit_count": 3}
        assert isinstance(row["after_val"]["hit_count"], int)
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_app_id=:app_id"), {"app_id": app_id}
            )
            await connection.execute(text("DELETE FROM app WHERE id=:id"), {"id": app_id})


@pytest.mark.asyncio
async def test_sensitive_audit_web_principal_matches_authenticated_context(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    principal = await _admin(owner, f"sens-web-{uuid4().hex[:10]}")
    store = SqlPipelineStore(_settings(accept_url))
    with correlation_scope(), audit_principal_scope(principal):
        await store.audit_sensitive_hit(principal, 2)
    async with owner.connect() as connection:
        row = (await connection.execute(text("""
            SELECT actor_subject_kind,actor_account_id,actor_identity_id,
              actor_app_id,role,after_val
            FROM audit_log WHERE action='sensitive_hit' AND actor_account_id=:account
        """), {"account": principal.account_id})).mappings().one()
    assert dict(row) == {
        "actor_subject_kind": "human", "actor_account_id": principal.account_id,
        "actor_identity_id": principal.identity_id, "actor_app_id": None,
        "role": "admin", "after_val": {"hit_count": 2},
    }
