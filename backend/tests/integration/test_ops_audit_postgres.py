"""队列恢复与人类 Raw 重放在真实 sms_accept + 审计触发器下的回归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.audit import insert_audit as real_insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from app.services.crypto import EncryptionContext
from app.services.ops_repository import SqlOpsRepository
from app.services.raw_lease import RawLeaseLost
from app.services.raw_parse import (
    ELIGIBILITY_AUTOMATIC,
    ELIGIBILITY_MANUAL,
    ELIGIBILITY_NEVER,
    PARSE_PROCESSED,
    PARSE_PROTOCOL_INVALID,
    PARSE_UNATTEMPTED,
    RAW_PARSER_VERSION,
    RawParseDisposition,
)
from app.services.raw_replay import (
    RawReplayConflict,
    RawReplayService,
    RawReplaySystemAuditIncomplete,
)

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

PRINCIPAL_KEY = bytes.fromhex("11" * 32)
SYSTEM_REALTIME_KEY = bytes.fromhex("22" * 32)
ROOT = Path(__file__).resolve().parents[3]


def _system_audit_function_sql() -> str:
    """从 schema.sql 取出当前触发器函数，供快照库尚未升到 0078 时安装。"""

    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    start = schema.index("CREATE OR REPLACE FUNCTION enforce_live_audit_principal()")
    end = schema.index("REVOKE ALL ON FUNCTION enforce_live_audit_principal()")
    return schema[start:end].rstrip().rstrip(";")


@pytest.fixture
async def accept_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(
            text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{password}'")
        )
        if database:
            await connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept')
            )
        existing = await connection.scalar(
            text(
                """
                SELECT key_material FROM audit_context_signing_key
                WHERE key_kind='principal'
                """
            )
        )
        if existing is None:
            await connection.execute(
                text(
                    """
                    INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at)
                    VALUES ('principal',:key,now())
                    """
                ),
                {"key": PRINCIPAL_KEY},
            )
            principal_key = PRINCIPAL_KEY
        else:
            principal_key = bytes(existing)
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: principal_key if name == "audit_context_key" else None,
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


@pytest.fixture
async def send_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(
            text(f"ALTER ROLE sms_send WITH LOGIN PASSWORD '{password}'")
        )
        if database:
            await connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_send')
            )
        await connection.execute(
            text(
                """
                INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at)
                VALUES ('system:realtime',:key,now())
                ON CONFLICT (key_kind) DO UPDATE
                  SET key_material=EXCLUDED.key_material,updated_at=now()
                """
            ),
            {"key": SYSTEM_REALTIME_KEY},
        )
        await connection.exec_driver_sql(_system_audit_function_sql())
        await connection.execute(text("GRANT INSERT ON audit_log TO sms_send"))
        await connection.execute(
            text("GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO sms_send")
        )
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: (
            SYSTEM_REALTIME_KEY
            if name == "audit_system_realtime_context_key"
            else PRINCIPAL_KEY
            if name == "audit_context_key"
            else None
        ),
    )
    send_url = owner_url.set(username="sms_send", password=password)
    try:
        yield owner, send_url
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _send_repository(send_url: URL) -> SqlOpsRepository:
    return SqlOpsRepository(
        cast(
            Any,
            SimpleNamespace(database_url=send_url, audit_producer_domain="realtime"),
        ),
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


async def _raw_effects(owner: AsyncEngine, raw_id: int) -> dict[str, object]:
    async with owner.connect() as connection:
        raw = (
            await connection.execute(
                text(
                    """
                    SELECT processed,item_count,replay_attempts,system_replay_audit_state
                    FROM raw_vendor_log WHERE id=:raw_id
                    """
                ),
                {"raw_id": raw_id},
            )
        ).mappings().one()
        events = (
            await connection.execute(
                text("SELECT count(*) FROM report_event WHERE raw_id=:raw_id"),
                {"raw_id": raw_id},
            )
        ).scalar_one()
        audits = (
            await connection.execute(
                text(
                    """
                    SELECT actor,actor_subject_kind,actor_account_id,
                      actor_identity_id,correlation_id,object_id,after_val
                    FROM audit_log
                    WHERE action='raw_replay'
                      AND object_type='raw_vendor_log'
                      AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                    ORDER BY id
                    """
                ),
                {"raw_id": raw_id},
            )
        ).mappings().all()
    return {
        "processed": bool(raw["processed"]),
        "item_count": int(raw["item_count"]),
        "replay_attempts": int(raw["replay_attempts"]),
        "system_replay_audit_state": raw["system_replay_audit_state"],
        "report_events": int(events),
        "audits": list(audits),
    }


class _ReplayCrypto:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[tuple[bytes, int]] = []

    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = True,
    ) -> bytes:
        del context, allow_legacy
        self.calls.append((payload, key_version))
        return self.raw


class _RecordingIngest:
    def __init__(self, owner: AsyncEngine) -> None:
        self.owner = owner
        self.calls: list[tuple[int, object]] = []

    async def process_existing(self, raw_id: int, data: object, **kwargs: object) -> int:
        self.calls.append((raw_id, data))
        count = len(data) if isinstance(data, list) else 0
        event_key = hashlib.sha256(f"replay-effect:{raw_id}".encode()).hexdigest()
        async with self.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE raw_vendor_log
                    SET processed=TRUE,item_count=:item_count,error=NULL,
                      processing_started_at=NULL,
                      parse_state='processed',replay_eligibility='never',
                      processing_lease_id=NULL,processing_lease_expires_at=NULL,
                      system_replay_audit_state=CASE
                        WHEN :system_audit_intent THEN 'pending'
                        ELSE system_replay_audit_state
                      END
                    WHERE id=:raw_id
                    """
                ),
                {
                    "raw_id": raw_id,
                    "item_count": count,
                    "system_audit_intent": bool(kwargs.get("system_audit_intent")),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO report_event(
                      event_key,raw_id,vendor_task_id,custom_id,phone_enc,
                      phone_hmac,phone_mask,key_version,report_status,
                      message_status,report_desc,report_time
                    ) VALUES (
                      :event_key,:raw_id,:vendor_task_id,:custom_id,:phone_enc,
                      :phone_hmac,'138****8000',1,1,'delivered','DELIVRD',now()
                    )
                    """
                ),
                {
                    "event_key": event_key,
                    "raw_id": raw_id,
                    "vendor_task_id": "b" * 64,
                    "custom_id": "c" * 64,
                    "phone_enc": b"ciphertext-only",
                    "phone_hmac": "d" * 64,
                },
            )
        return count


class _ForbiddenIngest:
    async def process_existing(self, raw_id: int, data: object, **_: object) -> int:
        raise AssertionError(f"reply ingest must not run for raw {raw_id}: {data!r}")


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
                      AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                    """
                ),
                {"raw_id": raw_id},
            )
            await connection.execute(
                text("DELETE FROM report_event WHERE raw_id=:raw_id"),
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

    attempts = {"n": 0}

    async def flaky(connection: object, event: object) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("synthetic audit failure")
        await real_insert_audit(connection, event)

    monkeypatch.setattr("app.services.ops_repository.insert_audit", flaky)
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
            failed_count = (
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
        assert int(failed_count) == 0

        with correlation_scope():
            resumed = await repository.resume_batches(
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
        assert [item.batch_no for item in resumed] == [batch_no]
        assert await _batch_status(owner, batch_no) == "queued"
        async with owner.connect() as connection:
            retry_row = (
                await connection.execute(
                    text(
                        """
                        SELECT actor,actor_subject_kind,actor_account_id,
                          actor_identity_id,after_val
                        FROM audit_log
                        WHERE action='queue_resume' AND actor_account_id=:account_id
                        """
                    ),
                    {"account_id": principal.account_id},
                )
            ).mappings().one()
        assert str(retry_row["actor"]) == login
        assert str(retry_row["actor_subject_kind"]) == "human"
        assert int(retry_row["actor_account_id"]) == principal.account_id
        assert int(retry_row["actor_identity_id"]) == principal.identity_id
        assert int(retry_row["after_val"]["resumed_batches"]) == 1
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
                              source,payload_enc,payload_sha256,processed,item_count,
                              parse_state,replay_eligibility
                            ) VALUES (
                              'report',:payload_enc,:sha,TRUE,2,
                              'processed','never'
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
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
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


@pytest.mark.asyncio
async def test_sms_accept_human_replay_audit_failure_retry_only_writes_audit(
    accept_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已提交的 processed 事实在审计失败后，重试只补人类审计、不重放业务。"""

    owner, accept_url = accept_runtime
    login = f"ops-replay-audit-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
    correlation_id = uuid4()
    items = [{"customId": "safe-custom-1"}, {"customId": "safe-custom-2"}]
    raw = json.dumps({"code": 0, "msg": "ok", "data": items}).encode()
    crypto = _ReplayCrypto(raw)
    ingest = _RecordingIngest(owner)
    repository = _repository(accept_url)
    service = RawReplayService(repository, crypto, ingest, _ForbiddenIngest())
    raw_id: int | None = None
    attempts = {"n": 0}

    async def flaky(connection: object, event: object) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("synthetic audit failure")
        await real_insert_audit(connection, event)

    monkeypatch.setattr("app.services.ops_repository.insert_audit", flaky)
    async with owner.begin() as connection:
        raw_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,processed,item_count
                        ) VALUES (
                          'report',:payload_enc,:sha,FALSE,0
                        ) RETURNING id
                        """
                    ),
                    {"payload_enc": b"ciphertext-only", "sha": hashlib.sha256(raw).hexdigest()},
                )
            ).scalar_one()
        )
    try:
        with correlation_scope(correlation_id), pytest.raises(RuntimeError, match="synthetic"):
            await service.replay(
                raw_id,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
        after_fail = await _raw_effects(owner, raw_id)
        assert after_fail["processed"] is True
        assert after_fail["item_count"] == 2
        assert after_fail["report_events"] == 1
        assert after_fail["audits"] == []
        assert [call[0] for call in ingest.calls] == [raw_id]
        assert isinstance(ingest.calls[0][1], list)
        assert len(ingest.calls[0][1]) == 2
        assert len(crypto.calls) == 1

        with correlation_scope(correlation_id):
            retried = await service.replay(
                raw_id,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
        after_retry = await _raw_effects(owner, raw_id)
        assert retried == 2
        assert after_retry["processed"] is True
        assert after_retry["item_count"] == 2
        assert after_retry["report_events"] == 1
        assert after_retry["replay_attempts"] == after_fail["replay_attempts"]
        assert len(ingest.calls) == 1
        assert len(crypto.calls) == 1
        assert len(after_retry["audits"]) == 1
        audit = after_retry["audits"][0]
        assert str(audit["actor"]) == login
        assert str(audit["actor_subject_kind"]) == "human"
        assert int(audit["actor_account_id"]) == principal.account_id
        assert int(audit["actor_identity_id"]) == principal.identity_id
        assert audit["correlation_id"] == correlation_id
        assert str(audit["object_id"]) == str(raw_id)
        assert audit["after_val"] == {"source": "report", "items": 2}

        with (
            correlation_scope(correlation_id),
            pytest.raises(RawReplayConflict, match="仅未处理"),
        ):
            await service.replay(
                raw_id,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
            )
        after_conflict = await _raw_effects(owner, raw_id)
        assert after_conflict["item_count"] == 2
        assert after_conflict["report_events"] == 1
        assert len(after_conflict["audits"]) == 1
        assert len(ingest.calls) == 1
        assert len(crypto.calls) == 1
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_reevaluate_audit_failure_leaves_parse_state_unchanged(
    accept_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-reeval-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
    repository = _repository(accept_url)
    raw_id: int | None = None

    async def boom(_connection: object, _event: object) -> None:
        raise RuntimeError("synthetic reevaluate audit failure")

    monkeypatch.setattr("app.services.ops_repository.insert_audit", boom)
    try:
        async with owner.begin() as connection:
            raw_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO raw_vendor_log(
                              source,payload_enc,payload_sha256,processed,
                              parse_state,replay_eligibility,error
                            ) VALUES (
                              'report',:payload_enc,:sha,FALSE,
                              'protocol_invalid','manual','VendorProtocolError'
                            ) RETURNING id
                            """
                        ),
                        {"payload_enc": b"ciphertext-only", "sha": "e" * 64},
                    )
                ).scalar_one()
            )
        disposition = RawParseDisposition(
            PARSE_UNATTEMPTED, ELIGIBILITY_AUTOMATIC, "decoded_ok"
        )
        with correlation_scope(), pytest.raises(RuntimeError, match="synthetic"):
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=False,
                expected_parse_state=PARSE_PROTOCOL_INVALID,
                expected_eligibility=ELIGIBILITY_MANUAL,
                disposition=disposition,
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_PROTOCOL_INVALID,
                    "replay_eligibility": ELIGIBILITY_MANUAL,
                    "processed": False,
                },
                after={
                    "parse_state": PARSE_UNATTEMPTED,
                    "replay_eligibility": ELIGIBILITY_AUTOMATIC,
                    "reason": "decoded_ok",
                    "parser_version": RAW_PARSER_VERSION,
                    "source": "report",
                },
            )
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT parse_state,replay_eligibility,error,processed
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            audits = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='raw_reevaluate'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).scalar_one()
        assert str(row["parse_state"]) == PARSE_PROTOCOL_INVALID
        assert str(row["replay_eligibility"]) == ELIGIBILITY_MANUAL
        assert bool(row["processed"]) is False
        assert int(audits) == 0
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_reevaluate_same_txn_audit_and_live_lease_conflict(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-reeval-ok-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
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
                              source,payload_enc,payload_sha256,processed,
                              parse_state,replay_eligibility
                            ) VALUES (
                              'report',:payload_enc,:sha,FALSE,
                              'protocol_invalid','manual'
                            ) RETURNING id
                            """
                        ),
                        {"payload_enc": b"ciphertext-only", "sha": "f" * 64},
                    )
                ).scalar_one()
            )
        disposition = RawParseDisposition(
            PARSE_UNATTEMPTED, ELIGIBILITY_AUTOMATIC, "decoded_ok"
        )
        after = {
            "parse_state": PARSE_UNATTEMPTED,
            "replay_eligibility": ELIGIBILITY_AUTOMATIC,
            "reason": "decoded_ok",
            "parser_version": RAW_PARSER_VERSION,
            "source": "report",
        }
        with correlation_scope():
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=False,
                expected_parse_state=PARSE_PROTOCOL_INVALID,
                expected_eligibility=ELIGIBILITY_MANUAL,
                disposition=disposition,
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_PROTOCOL_INVALID,
                    "replay_eligibility": ELIGIBILITY_MANUAL,
                    "processed": False,
                },
                after=after,
            )
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT parse_state,replay_eligibility
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            audit = (
                await connection.execute(
                    text(
                        """
                        SELECT actor,actor_subject_kind,actor_account_id,
                          actor_identity_id,after_val,before_val
                        FROM audit_log
                        WHERE action='raw_reevaluate'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
        assert str(row["parse_state"]) == PARSE_UNATTEMPTED
        assert str(row["replay_eligibility"]) == ELIGIBILITY_AUTOMATIC
        assert str(audit["actor"]) == login
        assert str(audit["actor_subject_kind"]) == "human"
        assert int(audit["actor_account_id"]) == principal.account_id
        assert int(audit["actor_identity_id"]) == principal.identity_id
        assert audit["after_val"]["parser_version"] == RAW_PARSER_VERSION
        assert "phone" not in str(audit["after_val"]).casefold()
        assert "ciphertext" not in str(audit["after_val"]).casefold()

        with correlation_scope():
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=False,
                expected_parse_state=PARSE_UNATTEMPTED,
                expected_eligibility=ELIGIBILITY_AUTOMATIC,
                disposition=disposition,
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_UNATTEMPTED,
                    "replay_eligibility": ELIGIBILITY_AUTOMATIC,
                    "processed": False,
                },
                after=after,
            )
        async with owner.connect() as connection:
            audit_count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='raw_reevaluate'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).scalar_one()
        assert int(audit_count) == 1

        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE raw_vendor_log
                    SET processing_lease_id=gen_random_uuid(),
                        processing_lease_epoch=processing_lease_epoch+1,
                        processing_lease_expires_at=now()+interval '15 minutes'
                    WHERE id=:raw_id
                    """
                ),
                {"raw_id": raw_id},
            )
        with pytest.raises(RawLeaseLost):
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=False,
                expected_parse_state=PARSE_UNATTEMPTED,
                expected_eligibility=ELIGIBILITY_AUTOMATIC,
                disposition=RawParseDisposition(
                    PARSE_UNATTEMPTED, ELIGIBILITY_MANUAL, "manual_review"
                ),
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_UNATTEMPTED,
                    "replay_eligibility": ELIGIBILITY_AUTOMATIC,
                    "processed": False,
                },
                after=after,
            )
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_reevaluate_processed_raw_cannot_return_to_replayable(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-reeval-proc-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
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
                              source,payload_enc,payload_sha256,processed,
                              parse_state,replay_eligibility
                            ) VALUES (
                              'report',:payload_enc,:sha,TRUE,
                              'processed','never'
                            ) RETURNING id
                            """
                        ),
                        {"payload_enc": b"ciphertext-only", "sha": "g" * 64},
                    )
                ).scalar_one()
            )
        with pytest.raises(RawReplayConflict, match="已处理 raw"):
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=True,
                expected_parse_state=PARSE_PROCESSED,
                expected_eligibility=ELIGIBILITY_NEVER,
                disposition=RawParseDisposition(
                    PARSE_UNATTEMPTED, ELIGIBILITY_AUTOMATIC, "decoded_ok"
                ),
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_PROCESSED,
                    "replay_eligibility": ELIGIBILITY_NEVER,
                    "processed": True,
                },
                after={
                    "parse_state": PARSE_UNATTEMPTED,
                    "replay_eligibility": ELIGIBILITY_AUTOMATIC,
                    "reason": "decoded_ok",
                    "parser_version": RAW_PARSER_VERSION,
                    "source": "report",
                },
            )
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT processed,parse_state,replay_eligibility
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            audits = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='raw_reevaluate'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).scalar_one()
        assert bool(row["processed"]) is True
        assert str(row["parse_state"]) == PARSE_PROCESSED
        assert str(row["replay_eligibility"]) == ELIGIBILITY_NEVER
        assert int(audits) == 0
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)


@pytest.mark.asyncio
async def test_sms_accept_reevaluate_expected_state_miss_does_not_audit(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    login = f"ops-reeval-cas-{uuid4().hex[:12]}"
    principal = await _create_admin(owner, login=login)
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
                              source,payload_enc,payload_sha256,processed,
                              parse_state,replay_eligibility
                            ) VALUES (
                              'report',:payload_enc,:sha,FALSE,
                              'protocol_invalid','manual'
                            ) RETURNING id
                            """
                        ),
                        {"payload_enc": b"ciphertext-only", "sha": "h" * 64},
                    )
                ).scalar_one()
            )
        with pytest.raises(RawReplayConflict, match="状态已变化"):
            await repository.apply_raw_reevaluation(
                raw_id,
                expected_processed=False,
                expected_parse_state=PARSE_UNATTEMPTED,
                expected_eligibility=ELIGIBILITY_AUTOMATIC,
                disposition=RawParseDisposition(
                    PARSE_UNATTEMPTED, ELIGIBILITY_AUTOMATIC, "decoded_ok"
                ),
                error=None,
                actor=principal.login_name,
                ip="10.0.0.8",
                principal=principal,
                before={
                    "parse_state": PARSE_PROTOCOL_INVALID,
                    "replay_eligibility": ELIGIBILITY_MANUAL,
                    "processed": False,
                },
                after={
                    "parse_state": PARSE_UNATTEMPTED,
                    "replay_eligibility": ELIGIBILITY_AUTOMATIC,
                    "reason": "decoded_ok",
                    "parser_version": RAW_PARSER_VERSION,
                    "source": "report",
                },
            )
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT parse_state,replay_eligibility
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            audits = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='raw_reevaluate'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).scalar_one()
        assert str(row["parse_state"]) == PARSE_PROTOCOL_INVALID
        assert str(row["replay_eligibility"]) == ELIGIBILITY_MANUAL
        assert int(audits) == 0
    finally:
        await _cleanup(owner, raw_id=raw_id, principal=principal)


@pytest.mark.asyncio
async def test_sms_send_system_raw_replay_audit_rewrites_after_trigger_or_connection_failure(
    send_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, send_url = send_runtime
    items = [{"customId": "safe-system-1"}, {"customId": "safe-system-2"}]
    raw = json.dumps({"code": 0, "msg": "ok", "data": items}).encode()
    crypto = _ReplayCrypto(raw)
    ingest = _RecordingIngest(owner)
    repository = _send_repository(send_url)
    service = RawReplayService(repository, crypto, ingest, _ForbiddenIngest())
    raw_id: int | None = None
    attempts = {"n": 0}

    async def flaky_bind(
        connection: object,
        *,
        actor_name: str,
        action: str,
        producer_domain: str | None = None,
    ) -> None:
        from app.core.runtime_resources import bind_connection_system_audit as real_bind

        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("synthetic system audit context failure")
        await real_bind(
            connection,
            actor_name=actor_name,
            action=action,
            producer_domain=producer_domain,
        )

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_system_audit",
        flaky_bind,
    )
    async with owner.begin() as connection:
        raw_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,processed,item_count,
                          parse_state,replay_eligibility
                        ) VALUES (
                          'report',:payload_enc,:sha,FALSE,0,
                          'unattempted','automatic'
                        ) RETURNING id
                        """
                    ),
                    {
                        "payload_enc": b"ciphertext-only",
                        "sha": hashlib.sha256(raw).hexdigest(),
                    },
                )
            ).scalar_one()
        )
    try:
        with correlation_scope(), pytest.raises(RawReplaySystemAuditIncomplete):
            await service.replay(
                raw_id,
                actor="system-reconcile",
                ip="127.0.0.1",
                system_producer=True,
            )
        after_fail = await _raw_effects(owner, raw_id)
        assert after_fail["processed"] is True
        assert after_fail["item_count"] == 2
        assert after_fail["system_replay_audit_state"] == "pending"
        assert after_fail["report_events"] == 1
        assert after_fail["audits"] == []
        assert len(ingest.calls) == 1
        assert len(crypto.calls) == 1

        with correlation_scope():
            retried = await service.replay(
                raw_id,
                actor="system-reconcile",
                ip="127.0.0.1",
                system_producer=True,
            )
        after_retry = await _raw_effects(owner, raw_id)
        assert retried == 2
        assert after_retry["processed"] is True
        assert after_retry["system_replay_audit_state"] == "completed"
        assert after_retry["replay_attempts"] == after_fail["replay_attempts"]
        assert len(ingest.calls) == 1
        assert len(crypto.calls) == 1
        assert len(after_retry["audits"]) == 1
        audit = after_retry["audits"][0]
        assert str(audit["actor"]) == "system-reconcile"
        assert str(audit["actor_subject_kind"]) == "system"
        assert audit["actor_account_id"] is None
        assert audit["after_val"]["source"] == "report"
        assert int(audit["after_val"]["items"]) == 2
        assert int(audit["after_val"]["lease_epoch"]) == 1
        assert audit["after_val"]["producer_domain"] == "realtime"
        assert "phone" not in json.dumps(audit["after_val"])

        with correlation_scope():
            await repository.audit_raw_replay(
                raw_id,
                source="report",
                items=2,
                actor="system-reconcile",
                ip="127.0.0.1",
                system_producer=True,
                lease_epoch=1,
            )
        after_unique = await _raw_effects(owner, raw_id)
        assert len(after_unique["audits"]) == 1
    finally:
        await _cleanup(owner, raw_id=raw_id)


@pytest.mark.asyncio
async def test_sms_accept_human_mark_processed_does_not_touch_system_audit_column(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    from uuid import UUID

    from app.services.raw_lease import RawProcessingLease
    from app.services.report_repository import SqlReportRepository

    owner, accept_url = accept_runtime
    reports = SqlReportRepository(
        cast(
            Any,
            SimpleNamespace(
                database_url=accept_url,
                database_url_for=lambda _role: accept_url,
            ),
        )
    )
    raw_id: int | None = None
    lease_id = UUID(int=441)
    try:
        async with owner.begin() as connection:
            raw_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO raw_vendor_log(
                              source,payload_enc,payload_sha256,processed,item_count,
                              parse_state,replay_eligibility,
                              processing_lease_id,processing_lease_epoch,
                              processing_lease_expires_at
                            ) VALUES (
                              'report',:payload_enc,:sha,FALSE,0,
                              'unattempted','automatic',
                              :lease_id,1,now()+interval '15 minutes'
                            ) RETURNING id
                            """
                        ),
                        {
                            "payload_enc": b"ciphertext-only",
                            "sha": "c" * 64,
                            "lease_id": str(lease_id),
                        },
                    )
                ).scalar_one()
            )
        lease = RawProcessingLease(raw_id, lease_id, 1)
        reports.remember_lease(lease)
        await reports.mark_processed(raw_id, lease=lease)
        effects = await _raw_effects(owner, raw_id)
        assert effects["processed"] is True
        assert effects["system_replay_audit_state"] is None
        assert effects["audits"] == []
    finally:
        await _cleanup(owner, raw_id=raw_id)
