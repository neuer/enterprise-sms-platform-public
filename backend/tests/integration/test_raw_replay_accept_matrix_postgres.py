"""正式 sms_accept HTTP 重放非空报文，并核验 unknown 处置审计与同一事务回滚。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import app.api.ops as ops_api
from app.api.ops import router as ops_router
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.correlation import REQUEST_ID_HEADER, CorrelationIdMiddleware, correlation_scope
from app.core.errors import ApiError, api_error_handler
from app.core.runtime_resources import (
    bind_connection_system_audit,
    close_runtime_resources,
)
from app.services.crypto import CryptoService, EncryptionContext
from app.services.ops_repository import SqlOpsRepository
from app.services.raw_lease import RawLeaseLost
from app.services.raw_replay import RawReplayService
from app.services.reply_ingest import ReplyIngestService
from app.services.reply_repository import SqlReplyRepository
from app.services.report_ingest import ReportIngestService
from app.services.report_repository import SqlReportRepository
from app.services.uncertain_resolution import UncertainResolutionService
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ
    or "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

PHONE = "13800138000"
AES = base64.b64encode(b"e" * 32).decode()
PRINCIPAL_KEY = bytes.fromhex("33" * 32)
CREATED_AT = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
REPORT_TIME = "2026-07-15T16:00:00+08:00"
CALLBACK_URL = "https://callback.invalid"
CALLBACK_SECRET = b"cipher-secret"


def _sqlstate(error: DBAPIError) -> str | None:
    state = getattr(error.orig, "sqlstate", None)
    if state:
        return str(state)
    return "42501" if "42501" in str(error) else None


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(AES, AES)


def _seal(
    crypto: CryptoService, source: str, data: list[dict[str, object]]
) -> tuple[bytes, str, int]:
    raw = json.dumps({"code": 0, "msg": "ok", "data": data}, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    sealed = crypto.encrypt_bound_bytes(
        raw,
        EncryptionContext(
            domain="vendor-raw",
            table="raw_vendor_log",
            column="payload_enc",
            object_id=f"{source}:{digest}",
        ),
    )
    return sealed.payload, digest, sealed.key_version


def _claims(principal: SecurityPrincipal, role: str = "admin") -> JwtClaims:
    return JwtClaims(
        principal.account_id,
        principal.identity_id,
        "local",
        principal.login_name,
        principal.login_name,
        principal.dept,
        role,  # type: ignore[arg-type]
        1,
        f"jti-{principal.account_id}",
        f"session-{principal.account_id}",
    )


class TokenFacade:
    def __init__(self, tokens: dict[str, JwtClaims]) -> None:
        self.tokens = tokens

    async def verify(self, token: str) -> JwtClaims:
        return self.tokens[token]


async def _create_admin(owner: AsyncEngine, login: str) -> SecurityPrincipal:
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
                        ) VALUES (
                          :account_id,:provider_id,:login,:login,:external
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "login": login,
                        "external": f"local:{login}",
                    },
                )
            ).scalar_one()
        )
    return SecurityPrincipal(account_id, identity_id, login, "平台部", "admin")


@pytest.fixture
async def accept_world(monkeypatch: pytest.MonkeyPatch) -> Any:
    owner_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
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
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: PRINCIPAL_KEY if name == "audit_context_key" else None,
    )
    accept_url = owner_url.set(username="sms_accept", password=password)
    nonce = uuid4().hex
    proposer = await _create_admin(owner, f"raw-p-{nonce[:8]}")
    confirmer = await _create_admin(owner, f"raw-c-{nonce[:8]}")
    revoked = await _create_admin(owner, f"raw-r-{nonce[:8]}")
    world = SimpleNamespace(
        owner=owner,
        accept_url=accept_url,
        crypto=_crypto(),
        nonce=nonce,
        proposer=proposer,
        confirmer=confirmer,
        revoked=revoked,
        settings=cast(Any, SimpleNamespace(database_url=accept_url)),
    )
    try:
        yield world
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _services(world: Any) -> tuple[RawReplayService, UncertainResolutionService, SqlOpsRepository]:
    repository = SqlOpsRepository(world.settings, redis=object())
    crypto = world.crypto
    replay = RawReplayService(
        repository,
        crypto,
        ReportIngestService(None, SqlReportRepository(world.settings), crypto),
        ReplyIngestService(None, SqlReplyRepository(world.settings), crypto),
    )
    return replay, UncertainResolutionService(crypto, world.settings), repository


def _app(world: Any, replay: RawReplayService, resolution: UncertainResolutionService) -> FastAPI:
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.include_router(ops_router)
    operator = SecurityPrincipal(
        world.proposer.account_id,
        world.proposer.identity_id,
        "operator01",
        "平台部",
        "operator",
    )
    facade = TokenFacade(
        {
            "proposer": _claims(world.proposer),
            "confirmer": _claims(world.confirmer),
            "revoked": _claims(world.revoked),
            "operator": _claims(operator, role="operator"),
        }
    )
    application.dependency_overrides[get_auth_facade] = lambda: facade
    application.dependency_overrides[ops_api.get_raw_replay_service] = lambda: replay
    application.dependency_overrides[ops_api._resolution_service] = lambda: resolution
    return application


async def _insert_raw(
    owner: AsyncEngine,
    *,
    source: str,
    payload: bytes,
    digest: str,
    key_version: int,
    capture_state: str = "complete",
    replay_eligibility: str | None = None,
    parse_state: str | None = None,
) -> int:
    async with owner.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,key_version,
                          capture_state,parse_state,replay_eligibility
                        ) VALUES (
                          :source,:payload,:digest,:version,:capture,
                          COALESCE(:parse_state,'unattempted'),
                          COALESCE(:eligibility,'manual')
                        ) RETURNING id
                        """
                    ),
                    {
                        "source": source,
                        "payload": payload,
                        "digest": digest,
                        "version": key_version,
                        "capture": capture_state,
                        "parse_state": parse_state,
                        "eligibility": replay_eligibility,
                    },
                )
            ).scalar_one()
        )


async def _seed_message(
    owner: AsyncEngine,
    crypto: CryptoService,
    *,
    app_id: int,
    custom_id: str,
    chunk_status: str,
    batch_status: str,
    message_count: int = 1,
) -> tuple[int, int, list[int]]:
    protected = crypto.protect_phone(PHONE)
    async with owner.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,channel,app_id,dept,content,
                          display_content_enc,send_content_enc,status,total,category
                        ) VALUES (
                          :batch_no,'api',:app_id,'平台部','[encrypted]',
                          :content,:content,:status,:total,'notice'
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": uuid4().hex,
                        "app_id": app_id,
                        "content": b"cipher-content",
                        "status": batch_status,
                        "total": message_count,
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
                          batch_id,chunk_no,custom_id,phone_count,status,submitted_at
                        ) VALUES (
                          :batch_id,1,:custom_id,:phones,:status,:submitted_at
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "custom_id": custom_id,
                        "phones": message_count,
                        "status": chunk_status,
                        "submitted_at": CREATED_AT,
                    },
                )
            ).scalar_one()
        )
        message_ids: list[int] = []
        for index in range(message_count):
            current = protected if index == 0 else crypto.protect_phone(f"1380013800{index}")
            message_ids.append(
                int(
                    (
                        await connection.execute(
                            text(
                                """
                                INSERT INTO sms_message(
                                  batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,
                                  key_version,status,created_at
                                ) VALUES (
                                  :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,
                                  :key_version,'sent',:created_at
                                ) RETURNING id
                                """
                            ),
                            {
                                "batch_id": batch_id,
                                "chunk_id": chunk_id,
                                "phone_enc": current.phone_enc,
                                "phone_hmac": current.phone_hmac,
                                "phone_mask": current.phone_mask,
                                "key_version": current.key_version,
                                "created_at": CREATED_AT,
                            },
                        )
                    ).scalar_one()
                )
            )
    return batch_id, chunk_id, message_ids


def _report_item(custom_id: str, description: str) -> dict[str, object]:
    return {
        "taskId": f"task{custom_id[:8]}",
        "customId": custom_id,
        "phone": PHONE,
        "reportStatus": 1,
        "reportDescription": description,
        "reportTime": REPORT_TIME,
    }


def _reply_item(custom_id: str, contents: str) -> dict[str, object]:
    return {
        "taskId": f"task{custom_id[:8]}",
        "customId": custom_id,
        "phone": PHONE,
        "contents": contents,
        "replyTime": REPORT_TIME,
    }


async def _post(
    client: httpx.AsyncClient,
    path: str,
    token: str | None,
    *,
    body: dict[str, object] | None = None,
    correlation: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if correlation is not None:
        headers[REQUEST_ID_HEADER] = correlation
    if body is None:
        return await client.post(path, headers=headers)
    return await client.post(path, headers=headers, json=body)


@pytest.mark.asyncio
async def test_sms_accept_login_cannot_widen_replay_or_audit_privileges(accept_world: Any) -> None:
    accept = create_async_engine(
        cast(URL, accept_world.accept_url),
        connect_args={"server_settings": {"search_path": "pg_catalog,public"}},
    )
    try:
        denied = (
            "UPDATE raw_vendor_log SET payload_enc=payload_enc WHERE false",
            "UPDATE sms_chunk SET status=status WHERE false",
            "UPDATE sms_chunk SET id=id WHERE false",
            "UPDATE report_event SET report_desc=report_desc WHERE false",
            "DELETE FROM report_event WHERE false",
            "UPDATE audit_log SET action=action WHERE false",
            "DELETE FROM audit_log WHERE false",
            "TRUNCATE TABLE audit_log",
            "CREATE TABLE sms_accept_ddl_probe(id integer)",
        )
        for statement in denied:
            async with accept.begin() as connection:
                with pytest.raises(DBAPIError) as caught:
                    await connection.execute(text(statement))
                assert _sqlstate(caught.value) == "42501"
        async with accept_world.owner.connect() as connection:
            metrics = await connection.scalar(
                text(
                    """
                    SELECT NOT has_table_privilege('sms_metrics','audit_log','INSERT')
                      AND NOT has_table_privilege('sms_metrics','sms_chunk','UPDATE')
                      AND NOT rolsuper
                    FROM pg_roles WHERE rolname='sms_accept'
                    """
                )
            )
        assert metrics is True
    finally:
        await accept.dispose()


@pytest.mark.asyncio
async def test_sms_accept_http_replays_empty_and_nonempty_raw(
    accept_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay, resolution, repository = _services(accept_world)
    monkeypatch.setattr(ops_api, "_resolution_service", lambda: resolution)
    application = _app(accept_world, replay, resolution)
    crypto = accept_world.crypto
    owner = accept_world.owner
    nonce = accept_world.nonce
    async with owner.begin() as connection:
        app_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,
                          callback_url,callback_secret_enc,callback_report_enabled,created_by
                        ) VALUES (
                          :name,'平台部',:api_key_hash,:prefix,
                          :url,:secret,true,'test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"raw-matrix-{nonce[:12]}",
                        "api_key_hash": "b" * 64,
                        "prefix": nonce[:8],
                        "url": CALLBACK_URL,
                        "secret": CALLBACK_SECRET,
                    },
                )
            ).scalar_one()
        )
    matched_custom = f"{nonce[:24]}{1:08d}"
    late_custom = f"{nonce[:24]}{2:08d}"
    append_custom = f"{nonce[:24]}{3:08d}"
    reply_custom = f"{nonce[:24]}{4:08d}"
    optout_custom = f"{nonce[:24]}{5:08d}"
    unmatched_custom = f"{nonce[:24]}{6:08d}"
    matched = await _seed_message(
        owner, crypto, app_id=app_id, custom_id=matched_custom,
        chunk_status="submitted", batch_status="sending",
    )
    late = await _seed_message(
        owner, crypto, app_id=app_id, custom_id=late_custom,
        chunk_status="unknown_terminal", batch_status="completed_unknown",
    )
    append = await _seed_message(
        owner, crypto, app_id=app_id, custom_id=append_custom,
        chunk_status="submitted", batch_status="sending", message_count=2,
    )
    reply_target = await _seed_message(
        owner, crypto, app_id=app_id, custom_id=reply_custom,
        chunk_status="submitted", batch_status="sending",
    )
    optout_target = await _seed_message(
        owner, crypto, app_id=app_id, custom_id=optout_custom,
        chunk_status="submitted", batch_status="sending",
    )
    async with owner.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO callback_task(
                  app_id,event,batch_id,url,callback_secret_enc,callback_secret_key_version
                ) VALUES (
                  :app_id,'message.report',:batch_id,:url,:secret,1
                )
                """
            ),
            {
                "app_id": app_id,
                "batch_id": append[0],
                "url": CALLBACK_URL,
                "secret": CALLBACK_SECRET,
            },
        )
        blacklist_before = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM blacklist WHERE source='reply_optout'")
                )
            ).scalar_one()
        )

    async def store(source: str, data: list[dict[str, object]], **extra: object) -> int:
        payload, digest, version = _seal(crypto, source, data)
        return await _insert_raw(
            owner, source=source, payload=payload, digest=digest, key_version=version, **extra
        )

    empty_report = await store("report", [])
    empty_reply = await store("reply", [])
    matched_raw = await store("report", [_report_item(matched_custom, "delivered")])
    late_raw = await store("report", [_report_item(late_custom, "late")])
    append_raw = await store("report", [_report_item(append_custom, "append")])
    unmatched_raw = await store("report", [_report_item(unmatched_custom, "orphan")])
    reply_raw = await store("reply", [_reply_item(reply_custom, "收到")])
    optout_raw = await store("reply", [_reply_item(optout_custom, "退订")])
    truncated_payload, truncated_digest, truncated_version = _seal(crypto, "report", [])
    truncated_raw = await _insert_raw(
        owner,
        source="report",
        payload=truncated_payload,
        digest=truncated_digest,
        key_version=truncated_version,
        capture_state="truncated",
    )
    corrupt_sha = "ab" * 32
    corrupt_plain = b'{"code":0,"msg":"ok","data":[]}'
    corrupt_sealed = crypto.encrypt_bound_bytes(
        corrupt_plain,
        EncryptionContext(
            domain="vendor-raw",
            table="raw_vendor_log",
            column="payload_enc",
            object_id=f"report:{corrupt_sha}",
        ),
    )
    corrupt_raw = await _insert_raw(
        owner,
        source="report",
        payload=corrupt_sealed.payload,
        digest=corrupt_sha,
        key_version=corrupt_sealed.key_version,
    )
    illegal_raw = await _insert_raw(
        owner,
        source="report",
        payload=truncated_payload,
        digest=truncated_digest,
        key_version=truncated_version,
        replay_eligibility="never",
        parse_state="protocol_invalid",
    )
    concurrent_raw = await store("report", [_report_item(matched_custom, "concurrent")])

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://sms.test") as client:
        anonymous = await _post(client, f"/api/v1/web/admin/raw-logs/{empty_report}/replay", None)
        assert anonymous.status_code == 401
        forbidden = await _post(
            client, f"/api/v1/web/admin/raw-logs/{empty_report}/replay", "operator"
        )
        assert forbidden.status_code == 403

        for raw_id in (
            empty_report, empty_reply, matched_raw, late_raw, append_raw,
            unmatched_raw, reply_raw, optout_raw,
        ):
            correlation = str(uuid4())
            evaluated = await _post(
                client,
                f"/api/v1/web/admin/raw-logs/{raw_id}/reevaluate",
                "proposer",
                correlation=correlation,
            )
            assert evaluated.status_code == 200, evaluated.text
            assert evaluated.json()["replay_eligibility"] in {"automatic", "manual"}
            played = await _post(
                client,
                f"/api/v1/web/admin/raw-logs/{raw_id}/replay",
                "proposer",
                correlation=correlation,
            )
            assert played.status_code == 200, played.text
            again = await _post(
                client, f"/api/v1/web/admin/raw-logs/{raw_id}/replay", "proposer"
            )
            assert again.status_code == 409
            assert again.json()["code"] == "STATE_CONFLICT"

        truncated = await _post(
            client, f"/api/v1/web/admin/raw-logs/{truncated_raw}/reevaluate", "proposer"
        )
        assert truncated.status_code == 409
        corrupt = await _post(
            client, f"/api/v1/web/admin/raw-logs/{corrupt_raw}/replay", "proposer"
        )
        assert corrupt.status_code == 409
        assert corrupt.json()["code"] == "STATE_CONFLICT"
        illegal = await _post(
            client, f"/api/v1/web/admin/raw-logs/{illegal_raw}/replay", "proposer"
        )
        assert illegal.status_code == 409

    async def replay_once() -> str:
        with correlation_scope(uuid4()):
            try:
                await replay.replay(
                    concurrent_raw,
                    actor=accept_world.proposer.login_name,
                    ip="192.0.2.20",
                    principal=accept_world.proposer,
                )
            except Exception as error:
                return type(error).__name__
            return "ok"

    concurrent = await asyncio.gather(replay_once(), replay_once())
    assert concurrent.count("ok") == 1

    async with owner.connect() as connection:
        async def raw_row(raw_id: int) -> Any:
            return (
                await connection.execute(
                    text(
                        """
                        SELECT processed,parse_state,replay_eligibility,item_count
                        FROM raw_vendor_log WHERE id=:id
                        """
                    ),
                    {"id": raw_id},
                )
            ).mappings().one()

        empty = await raw_row(empty_report)
        assert bool(empty["processed"]) is True
        assert str(empty["parse_state"]) == "processed"
        assert str(empty["replay_eligibility"]) == "never"
        assert int(empty["item_count"]) == 0
        reply_empty = await raw_row(empty_reply)
        assert bool(reply_empty["processed"]) is True
        assert int(reply_empty["item_count"]) == 0

        projection = (
            await connection.execute(
                text(
                    """
                    SELECT m.status,p.projection_changed
                    FROM report_event e
                    JOIN report_event_projection p ON p.event_key=e.event_key
                    JOIN sms_message m ON m.id=p.message_id AND m.created_at=p.message_created_at
                    WHERE e.raw_id=:raw_id
                    """
                ),
                {"raw_id": matched_raw},
            )
        ).mappings().one()
        assert str(projection["status"]) == "delivered"
        assert bool(projection["projection_changed"]) is True
        assert int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM report_event WHERE raw_id=:id"),
                    {"id": matched_raw},
                )
            ).scalar_one()
        ) == 1
        assert int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event
                        WHERE event_type='callback.ready'
                          AND aggregate_id IN (
                            SELECT CAST(id AS text) FROM callback_task WHERE batch_id=:batch
                          )
                        """
                    ),
                    {"batch": matched[0]},
                )
            ).scalar_one()
        ) >= 1

        late_chunk = (
            await connection.execute(
                text(
                    """
                    SELECT status,late_evidence_at IS NOT NULL AS late
                    FROM sms_chunk WHERE id=:id
                    """
                ),
                {"id": late[1]},
            )
        ).mappings().one()
        assert str(late_chunk["status"]) == "unknown_terminal"
        assert bool(late_chunk["late"]) is True

        unmatched_count = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM unmatched_report u
                        JOIN report_event e ON e.event_key=u.event_key
                        WHERE e.raw_id=:raw_id
                        """
                    ),
                    {"raw_id": unmatched_raw},
                )
            ).scalar_one()
        )
        assert unmatched_count == 1
        assert int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM report_event_projection p
                        JOIN report_event e ON e.event_key=p.event_key
                        WHERE e.raw_id=:id
                        """
                    ),
                    {"id": unmatched_raw},
                )
            ).scalar_one()
        ) == 0

        reply_flag = (
            await connection.execute(
                text(
                    """
                    SELECT e.is_optout,count(r.id) replies
                    FROM reply_event e
                    LEFT JOIN sms_reply r ON r.event_key=e.event_key
                    WHERE e.raw_id=:raw_id
                    GROUP BY e.is_optout
                    """
                ),
                {"raw_id": reply_raw},
            )
        ).mappings().one()
        assert bool(reply_flag["is_optout"]) is False
        assert int(reply_flag["replies"]) == 1
        optout_flag = (
            await connection.execute(
                text("SELECT is_optout FROM reply_event WHERE raw_id=:raw_id"),
                {"raw_id": optout_raw},
            )
        ).scalar_one()
        assert bool(optout_flag) is True
        blacklist_after = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM blacklist WHERE source='reply_optout'")
                )
            ).scalar_one()
        )
        assert blacklist_after == blacklist_before

        appended = (
            await connection.execute(
                text(
                    """
                    SELECT count(*) tasks,max(cardinality(message_ids)) refs
                    FROM callback_task
                    WHERE batch_id=:batch AND event='message.report'
                    """
                ),
                {"batch": append[0]},
            )
        ).mappings().one()
        assert int(appended["tasks"]) == 1
        assert int(appended["refs"]) == 1
        created_tasks = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM callback_task
                        WHERE batch_id=:batch AND event='message.report'
                        """
                    ),
                    {"batch": matched[0]},
                )
            ).scalar_one()
        )
        assert created_tasks == 1

        concurrent_events = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM report_event WHERE raw_id=:id"),
                    {"id": concurrent_raw},
                )
            ).scalar_one()
        )
        assert concurrent_events == 1
        for raw_id in (matched_raw, reply_raw, optout_raw, late_raw, empty_report):
            audits = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM audit_log
                            WHERE action='raw_replay'
                              AND object_type='raw_vendor_log'
                              AND object_id=CAST(CAST(:id AS bigint) AS text)
                            """
                        ),
                        {"id": raw_id},
                    )
                ).scalar_one()
            )
            assert audits == 1
        corrupt_state = await raw_row(corrupt_raw)
        assert bool(corrupt_state["processed"]) is False
        assert str(late_chunk["status"]) != "pending"
        assert int(reply_target[1]) > 0
        assert int(optout_target[1]) > 0

    claim = await repository.claim_raw_for_replay(
        await store("report", []),
        principal=accept_world.proposer,
        ip="192.0.2.21",
    )
    assert claim is not None and claim.lease is not None
    async with owner.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE raw_vendor_log
                SET processing_lease_id=gen_random_uuid(),
                    processing_lease_epoch=processing_lease_epoch+1
                WHERE id=:id
                """
            ),
            {"id": claim.record.id},
        )
    with pytest.raises(RawLeaseLost):
        await SqlReportRepository(accept_world.settings).update_metadata(
            claim.record.id,
            custom_ids=[],
            item_count=4,
            lease=claim.lease,
        )
    async with owner.connect() as connection:
        untouched = int(
            (
                await connection.execute(
                    text("SELECT item_count FROM raw_vendor_log WHERE id=:id"),
                    {"id": claim.record.id},
                )
            ).scalar_one()
        )
    assert untouched == 0


@pytest.mark.asyncio
async def test_sms_accept_http_resolution_audit_rolls_back_with_business(
    accept_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay, resolution, _repository = _services(accept_world)
    monkeypatch.setattr(ops_api, "_resolution_service", lambda: resolution)
    application = _app(accept_world, replay, resolution)
    owner = accept_world.owner
    nonce = accept_world.nonce

    async def seed_chunk(index: int) -> int:
        async with owner.begin() as connection:
            batch_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_batch(
                              batch_no,channel,dept,content,
                              display_content_enc,send_content_enc,status,category
                            ) VALUES (
                              :batch_no,'web','平台部','[encrypted]',
                              :content,:content,'completed_unknown','notice'
                            ) RETURNING id
                            """
                        ),
                        {"batch_no": uuid4().hex, "content": b"cipher-content"},
                    )
                ).scalar_one()
            )
            return int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_chunk(
                              batch_id,chunk_no,custom_id,phone_count,status
                            ) VALUES (
                              :batch_id,1,:custom_id,1,'unknown_terminal'
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "custom_id": f"{nonce[:24]}{index:08d}",
                        },
                    )
                ).scalar_one()
            )

    chunks = {action: await seed_chunk(index) for index, action in enumerate(
        ("confirm_accepted", "confirm_not_accepted", "keep_unknown", "resend_new_batch"),
        start=20,
    )}
    failure_chunk = await seed_chunk(30)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://sms.test") as client:
        missing = await _post(
            client,
            f"/api/v1/web/admin/chunks/{chunks['keep_unknown']}/resolution",
            None,
            body={"action": "keep_unknown"},
        )
        assert missing.status_code == 401
        operator = await _post(
            client,
            f"/api/v1/web/admin/chunks/{chunks['keep_unknown']}/resolution",
            "operator",
            body={"action": "keep_unknown"},
        )
        assert operator.status_code == 403
        proposed: dict[str, int] = {}
        for action, chunk_id in chunks.items():
            response = await _post(
                client,
                f"/api/v1/web/admin/chunks/{chunk_id}/resolution",
                "proposer",
                body={"action": action},
                correlation=str(uuid4()),
            )
            assert response.status_code == 200, response.text
            proposed[action] = int(response.json()["id"])
            assert response.json()["state"] == "proposed"
        same = await _post(
            client,
            f"/api/v1/web/admin/resolutions/{proposed['keep_unknown']}/confirm",
            "proposer",
        )
        assert same.status_code == 409
        assert same.json()["code"] == "STATE_CONFLICT"
        confirmed = await _post(
            client,
            f"/api/v1/web/admin/resolutions/{proposed['keep_unknown']}/confirm",
            "confirmer",
            correlation=str(uuid4()),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["state"] == "effect_pending"
        repeated = await _post(
            client,
            f"/api/v1/web/admin/resolutions/{proposed['keep_unknown']}/confirm",
            "confirmer",
        )
        assert repeated.status_code == 409

        async with owner.begin() as connection:
            await connection.execute(
                text("UPDATE user_account SET status=0 WHERE id=:id"),
                {"id": accept_world.revoked.account_id},
            )
        revoked = await _post(
            client,
            f"/api/v1/web/admin/resolutions/{proposed['confirm_accepted']}/confirm",
            "revoked",
        )
        assert revoked.status_code == 403

        from app.core.audit import insert_audit as real_insert_audit

        async def fail_confirm(connection: object, event: Any) -> None:
            if event.action == "uncertain_resolve_confirm":
                raise RuntimeError("synthetic confirm audit failure")
            await real_insert_audit(connection, event)

        monkeypatch.setattr(
            "app.services.uncertain_resolution_decision.insert_audit", fail_confirm
        )
        with pytest.raises(RuntimeError, match="synthetic confirm audit failure"):
            await _post(
                client,
                f"/api/v1/web/admin/resolutions/{proposed['resend_new_batch']}/confirm",
                "confirmer",
            )

        async def fail_propose(connection: object, event: Any) -> None:
            if event.action == "uncertain_resolve_propose":
                raise RuntimeError("synthetic propose audit failure")
            await real_insert_audit(connection, event)

        monkeypatch.setattr(
            "app.services.uncertain_resolution_decision.insert_audit", fail_propose
        )
        with pytest.raises(RuntimeError, match="synthetic propose audit failure"):
            await _post(
                client,
                f"/api/v1/web/admin/chunks/{failure_chunk}/resolution",
                "proposer",
                body={"action": "keep_unknown"},
            )

    async with owner.connect() as connection:
        outbox = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event
                        WHERE event_type='uncertain.effect'
                          AND aggregate_id=ANY(:ids)
                        """
                    ),
                    {"ids": [str(value) for value in proposed.values()]},
                )
            ).scalar_one()
        )
        assert outbox == 1
        kept = (
            await connection.execute(
                text(
                    """
                    SELECT r.state,c.status
                    FROM sms_uncertain_resolution r
                    JOIN sms_chunk c ON c.id=r.chunk_id
                    WHERE r.id=:id
                    """
                ),
                {"id": proposed["keep_unknown"]},
            )
        ).mappings().one()
        assert str(kept["state"]) == "effect_pending"
        assert str(kept["status"]) == "unknown_terminal"
        resend_state = str(
            (
                await connection.execute(
                    text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
                    {"id": proposed["resend_new_batch"]},
                )
            ).scalar_one()
        )
        assert resend_state == "proposed"
        assert int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event
                        WHERE event_type='uncertain.effect'
                          AND aggregate_id=:id
                        """
                    ),
                    {"id": str(proposed["resend_new_batch"])},
                )
            ).scalar_one()
        ) == 0
        failed_rows = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM sms_uncertain_resolution WHERE chunk_id=:id"),
                    {"id": failure_chunk},
                )
            ).scalar_one()
        )
        assert failed_rows == 0
        propose_audit = (
            await connection.execute(
                text(
                    """
                    SELECT actor,actor_account_id,before_val,after_val
                    FROM audit_log
                    WHERE action='uncertain_resolve_propose'
                      AND object_id=CAST(CAST(:id AS bigint) AS text)
                    """
                ),
                {"id": proposed["keep_unknown"]},
            )
        ).mappings().one()
        assert str(propose_audit["actor"]) == accept_world.proposer.login_name
        assert int(propose_audit["actor_account_id"]) == accept_world.proposer.account_id
        assert propose_audit["before_val"]["status"] == "unknown_terminal"
        assert propose_audit["after_val"]["status"] == "proposed"
        assert propose_audit["after_val"]["action"] == "keep_unknown"
        assert "phone" not in json.dumps(propose_audit["before_val"])
        assert "phone" not in json.dumps(propose_audit["after_val"])
        confirm_audit = (
            await connection.execute(
                text(
                    """
                    SELECT actor,actor_account_id,before_val,after_val
                    FROM audit_log
                    WHERE action='uncertain_resolve_confirm'
                      AND object_id=CAST(CAST(:id AS bigint) AS text)
                    """
                ),
                {"id": proposed["keep_unknown"]},
            )
        ).mappings().one()
        assert str(confirm_audit["actor"]) == accept_world.confirmer.login_name
        assert confirm_audit["before_val"]["status"] == "proposed"
        assert confirm_audit["after_val"]["status"] == "effect_pending"
        assert int(confirm_audit["after_val"]["confirmer_account_id"]) == (
            accept_world.confirmer.account_id
        )
        confirm_count = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM audit_log
                        WHERE action='uncertain_resolve_confirm'
                          AND object_id=CAST(CAST(:id AS bigint) AS text)
                        """
                    ),
                    {"id": proposed["resend_new_batch"]},
                )
            ).scalar_one()
        )
        assert confirm_count == 0
        pending_chunks = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM sms_chunk
                        WHERE id=ANY(:ids) AND status='pending'
                        """
                    ),
                    {"ids": [*chunks.values(), failure_chunk]},
                )
            ).scalar_one()
        )
        assert pending_chunks == 0
