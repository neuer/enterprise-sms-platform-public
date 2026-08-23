"""队列恢复与人类 Raw 重放在真实 sms_accept + 审计触发器下的回归。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from app.services.ops_repository import SqlOpsRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

PRINCIPAL_KEY = bytes.fromhex("11" * 32)


@pytest.fixture
async def accept_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    async with owner.begin() as connection:
        await connection.execute(
            text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{password}'")
        )
        await connection.execute(
            text(
                """
                INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at)
                VALUES ('principal',:key,now())
                ON CONFLICT (key_kind) DO UPDATE
                SET key_material=EXCLUDED.key_material,updated_at=now()
                """
            ),
            {"key": PRINCIPAL_KEY},
        )
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: PRINCIPAL_KEY if name == "audit_context_key" else None,
    )
    accept_url = owner_url.set(username="sms_accept", password=password)
    try:
        yield owner, accept_url
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _repository(accept_url: URL) -> SqlOpsRepository:
    return SqlOpsRepository(
        cast(Any, SimpleNamespace(database_url=accept_url)),
        redis=object(),
    )


async def _create_admin(owner: AsyncEngine, *, login: str) -> SecurityPrincipal:
    async with owner.begin() as connection:
        account_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO user_account(display_name,dept,role)
                        VALUES(:login,'平台部','admin') RETURNING id
                        """
                    ),
                    {"login": login},
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
    return SecurityPrincipal(account_id, identity_id, login, "平台部", "admin")


async def _insert_blocked_batch(owner: AsyncEngine, batch_no: str) -> None:
    async with owner.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO sms_batch(
                  batch_no,category,channel,dept,content,
                  display_content_enc,send_content_enc,status
                ) VALUES(
                  :batch_no,'notice','web','平台部','[encrypted]',
                  :ciphertext,:ciphertext,'balance_blocked'
                )
                """
            ),
            {"batch_no": batch_no, "ciphertext": b"ciphertext-only"},
        )


async def _batch_status(owner: AsyncEngine, batch_no: str) -> str:
    async with owner.connect() as connection:
        return str(
            (
                await connection.execute(
                    text("SELECT status FROM sms_batch WHERE batch_no=:batch_no"),
                    {"batch_no": batch_no},
                )
            ).scalar_one()
        )


async def _cleanup(
    owner: AsyncEngine,
    *,
    batch_no: str | None = None,
    raw_id: int | None = None,
    principal: SecurityPrincipal | None = None,
) -> None:
    async with owner.begin() as connection:
        if batch_no is not None:
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no=:batch_no"),
                {"batch_no": batch_no},
            )
        if raw_id is not None:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE action='raw_replay'
                      AND object_type='raw_vendor_log'
                      AND object_id=CAST(:raw_id AS text)
                    """
                ),
                {"raw_id": raw_id},
            )
            await connection.execute(
                text("DELETE FROM raw_vendor_log WHERE id=:raw_id"),
                {"raw_id": raw_id},
            )
        if principal is not None:
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": principal.account_id},
            )
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:identity_id"),
                {"identity_id": principal.identity_id},
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:account_id"),
                {"account_id": principal.account_id},
            )


@pytest.mark.asyncio
async def test_sms_accept_trigger_rejects_unattributed_audit_and_forged_actor(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-forged-{uuid4().hex[:12]}"
    batch_no = uuid4().hex
    principal = await _create_admin(owner, login=login)
    repository = _repository(accept_url)
    accept_engine = create_async_engine(accept_url)
    try:
        await _insert_blocked_batch(owner, batch_no)
        async with accept_engine.begin() as connection:
            with pytest.raises(DBAPIError, match="authenticated actor context"):
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(actor,action,object_type,object_id)
                        VALUES('display-only','unattributed_probe','probe','1')
                        """
                    )
                )
        with correlation_scope(), pytest.raises(RuntimeError, match="audit principal"):
            await repository.resume_batches(
                actor="forged-admin",
                ip="10.0.0.8",
                principal=principal,
            )
        assert await _batch_status(owner, batch_no) == "balance_blocked"
        async with owner.connect() as connection:
            count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='queue_resume' AND actor_account_id=:account_id
                        """
                    ),
                    {"account_id": principal.account_id},
                )
            ).scalar_one()
        assert int(count) == 0
    finally:
        await accept_engine.dispose()
        await _cleanup(owner, batch_no=batch_no, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_queue_resume_audit_failure_leaves_queue_unchanged(
    accept_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-rollback-{uuid4().hex[:12]}"
    batch_no = uuid4().hex
    principal = await _create_admin(owner, login=login)
    repository = _repository(accept_url)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr("app.services.ops_repository.insert_audit", boom)
    try:
        await _insert_blocked_batch(owner, batch_no)
        with correlation_scope(), pytest.raises(RuntimeError, match="synthetic"):
            await repository.resume_batches(
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
        assert await _batch_status(owner, batch_no) == "balance_blocked"
        async with owner.connect() as connection:
            count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='queue_resume' AND actor_account_id=:account_id
                        """
                    ),
                    {"account_id": principal.account_id},
                )
            ).scalar_one()
        assert int(count) == 0
    finally:
        await _cleanup(owner, batch_no=batch_no, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_queue_resume_retry_and_concurrency_write_jwt_audit(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-resume-{uuid4().hex[:12]}"
    batch_no = uuid4().hex
    principal = await _create_admin(owner, login=login)
    correlation_id = uuid4()
    repository = _repository(accept_url)
    try:
        await _insert_blocked_batch(owner, batch_no)
        with correlation_scope(correlation_id):
            first, second = await asyncio.gather(
                repository.resume_batches(
                    actor=principal.login_name,
                    ip="10.0.0.8",
                    principal=principal,
                ),
                repository.resume_batches(
                    actor=principal.login_name,
                    ip="10.0.0.8",
                    principal=principal,
                ),
            )
        resumed = (*first, *second)
        assert len(resumed) == 1
        assert resumed[0].batch_no == batch_no
        assert await _batch_status(owner, batch_no) == "queued"
        async with owner.connect() as connection:
            audits = (
                await connection.execute(
                    text(
                        """
                        SELECT actor,actor_subject_kind,actor_account_id,
                          actor_identity_id,correlation_id,after_val
                        FROM audit_log
                        WHERE action='queue_resume' AND actor_account_id=:account_id
                        ORDER BY id
                        """
                    ),
                    {"account_id": principal.account_id},
                )
            ).mappings().all()
        assert audits
        assert all(str(item["actor"]) == login for item in audits)
        assert all(str(item["actor_subject_kind"]) == "human" for item in audits)
        assert all(int(item["actor_account_id"]) == principal.account_id for item in audits)
        assert all(int(item["actor_identity_id"]) == principal.identity_id for item in audits)
        assert all(item["correlation_id"] == correlation_id for item in audits)
        assert sum(int(item["after_val"]["resumed_batches"]) for item in audits) == 1
    finally:
        await _cleanup(owner, batch_no=batch_no, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_human_raw_replay_audit_retry_keeps_processed_fact(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-replay-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
    correlation_id = uuid4()
    repository = _repository(accept_url)
    raw_id: int | None = None
    try:
        async with owner.begin() as connection:
            raw_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO raw_vendor_log(
                              source,payload_enc,payload_sha256,processed,item_count
                            ) VALUES (
                              'report',:payload_enc,:sha,TRUE,2
                            ) RETURNING id
                            """
                        ),
                        {"payload_enc": b"ciphertext-only", "sha": "a" * 64},
                    )
                ).scalar_one()
            )

        assert await repository.has_human_raw_replay_audit(raw_id) is False
        with (
            correlation_scope(correlation_id),
            pytest.raises(RuntimeError, match="audit principal"),
        ):
            await repository.audit_raw_replay(
                raw_id,
                source="report",
                items=2,
                actor="forged-admin",
                ip="10.0.0.8",
                principal=principal,
            )
        assert await repository.has_human_raw_replay_audit(raw_id) is False

        with correlation_scope(correlation_id):
            await repository.audit_raw_replay(
                raw_id,
                source="report",
                items=2,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
            await asyncio.gather(
                repository.audit_raw_replay(
                    raw_id,
                    source="report",
                    items=2,
                    actor=principal.login_name,
                    ip="10.0.0.8",
                    principal=principal,
                ),
                repository.audit_raw_replay(
                    raw_id,
                    source="report",
                    items=2,
                    actor=principal.login_name,
                    ip="10.0.0.8",
                    principal=principal,
                ),
            )
        assert await repository.has_human_raw_replay_audit(raw_id) is True
        async with owner.connect() as connection:
            processed = (
                await connection.execute(
                    text(
                        "SELECT processed,item_count FROM raw_vendor_log WHERE id=:raw_id"
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT actor,actor_subject_kind,actor_account_id,
                          actor_identity_id,correlation_id,object_id,after_val
                        FROM audit_log
                        WHERE action='raw_replay'
                          AND object_type='raw_vendor_log'
                          AND object_id=CAST(:raw_id AS text)
                        ORDER BY id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().all()
        assert processed["processed"] is True
        assert int(processed["item_count"]) == 2
        assert rows
        assert all(str(row["actor"]) == login for row in rows)
        assert all(str(row["actor_subject_kind"]) == "human" for row in rows)
        assert all(int(row["actor_account_id"]) == principal.account_id for row in rows)
        assert all(int(row["actor_identity_id"]) == principal.identity_id for row in rows)
        assert all(row["correlation_id"] == correlation_id for row in rows)
        assert all(str(row["object_id"]) == str(raw_id) for row in rows)
        assert all(row["after_val"]["source"] == "report" for row in rows)
        assert all(int(row["after_val"]["items"]) == 2 for row in rows)
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)
