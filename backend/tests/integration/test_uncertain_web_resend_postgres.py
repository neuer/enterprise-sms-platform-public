from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.apikey import SqlApiKeyRepository
from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.crypto import CryptoService, EncryptionContext
from app.services.idempotency import IdempotencyCoordinator
from app.services.pipeline import PipelineConfig, SendPipeline
from app.services.pipeline_repository import SqlPipelineStore
from app.services.uncertain_resolution import UncertainResolutionService
from app.services.usage_ledger import UsageLedgerService
from app.services.usage_subject import SYSTEM_UNCERTAIN_RESEND_APP_NAME
from scripts_support.maintain_partitions import maintain
from tests.integration.test_usage_ledger_postgres import ProjectionRedis

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)

_AES = __import__("base64").b64encode(b"v" * 32).decode()
_HMAC = __import__("base64").b64encode(b"v" * 32).decode()


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(_AES, _HMAC)


class EngineBoundStore(SqlPipelineStore):
    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(settings=settings)
        self._bound_engine = engine
        sync_engine = engine.sync_engine
        if not getattr(sync_engine, "_sms_uncertain_audit_begin", False):
            event.listen(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_uncertain_audit_begin = True

    def _engine(self) -> Any:
        return self._bound_engine

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        return set()

    async def sensitive_hits(self, content: str) -> list[str]:
        return []


class BoundLedger(UsageLedgerService):
    def __init__(self, redis: Any, settings: Any, engine: Any) -> None:
        super().__init__(redis, settings, pooled=False, clock=lambda: datetime.now(UTC))
        self._bound_engine = engine

    def _engine(self) -> Any:
        return self._bound_engine


class _NoDisposeEngine:
    """apply_effect 会 dispose；测试共享 engine 不能被关掉。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def begin(self) -> Any:
        return self._engine.begin()

    def connect(self) -> Any:
        return self._engine.connect()

    async def dispose(self) -> None:
        return None


class BoundResolution(UncertainResolutionService):
    def __init__(self, crypto: CryptoService, engine: Any) -> None:
        super().__init__(crypto, settings=cast(Any, SimpleNamespace(database_url="unused")))
        self._bound_engine = _NoDisposeEngine(engine)

    def _engine(self) -> Any:
        return self._bound_engine


class FakeFrequency:
    async def allow(self, category: str, **_values: Any) -> bool:
        return True


class FakeQuota:
    async def reserve(self, **_values: Any) -> None:
        return None

    async def refund(self, **_values: Any) -> None:
        return None

    async def refund_reservation(self, **_values: Any) -> None:
        return None


class FakePublisher:
    async def enqueue(self, batch_no: str, queue: str) -> None:
        return None


async def _prepare_db(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _create_admin(engine: Any, login: str) -> SecurityPrincipal:
    async with engine.begin() as connection:
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
            (await connection.execute(text("SELECT id FROM auth_provider WHERE code='local'")))
            .scalar_one()
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
                          :account_id,:provider_id,:login,:login,:external
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "login": login.lower(),
                        "external": f"local:{login.lower()}",
                    },
                )
            ).scalar_one()
        )
    return SecurityPrincipal(account_id, identity_id, login, "平台部", "admin")


async def _insert_api_app(engine: Any, nonce: str, *, status: int = 1) -> int:
    async with engine.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,
                          allowed_categories,daily_quota,status,created_by
                        ) VALUES(
                          :name,'平台部',:digest,:prefix,'notice',100,:status,'test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"api-{nonce}",
                        "digest": "c" * 64,
                        "prefix": nonce[:8],
                        "status": status,
                    },
                )
            ).scalar_one()
        )


async def _seed_unknown_batch(
    engine: Any,
    crypto: CryptoService,
    *,
    channel: str,
    dept: str,
    app_id: int | None,
) -> tuple[int, int]:
    batch_no = uuid4().hex
    content = crypto.encrypt_bound_packed_text(
        "通知内容",
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id=batch_no,
        ),
    )
    display = crypto.encrypt_bound_packed_text(
        "通知内容",
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=batch_no,
        ),
    )
    phone = crypto.protect_phone("13800138000")
    custom_id = uuid4().hex
    async with engine.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,category,channel,app_id,creator,dept,content,
                          display_content_enc,send_content_enc,segments,quota_cost,
                          status,total,unknown_cnt
                        ) VALUES(
                          :batch_no,'notice',:channel,:app_id,'ops',:dept,
                          '[encrypted]',:display,:content,1,1,'completed_unknown',1,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "channel": channel,
                        "app_id": app_id,
                        "dept": dept,
                        "display": display,
                        "content": content,
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
                        ) VALUES(:batch_id,1,:custom_id,1,'unknown_terminal')
                        RETURNING id
                        """
                    ),
                    {"batch_id": batch_id, "custom_id": custom_id},
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO sms_message(
                  batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,key_version,status
                ) VALUES(
                  :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,:key_version,
                  'unknown'
                )
                """
            ),
            {
                "batch_id": batch_id,
                "chunk_id": chunk_id,
                "phone_enc": phone.phone_enc,
                "phone_hmac": phone.phone_hmac,
                "phone_mask": phone.phone_mask,
                "key_version": phone.key_version,
            },
        )
    return batch_id, chunk_id


def _pipeline(
    store: EngineBoundStore,
    ledger: BoundLedger,
    crypto: CryptoService,
    redis: Redis,
) -> SendPipeline:
    return SendPipeline(
        store=store,
        idempotency=IdempotencyCoordinator(redis, store),
        crypto=crypto,
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(dept_daily_quota=0),
    )


@pytest_asyncio.fixture
async def resend_env() -> Any:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    await _prepare_db(engine)
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            redis_control_url=os.environ["AUTH_GUARD_REDIS_URL"],
            vendor_mock=True,
        ),
    )
    store = EngineBoundStore(engine, settings)
    ledger = BoundLedger(ProjectionRedis(), settings, engine)
    crypto = _crypto()
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    try:
        yield SimpleNamespace(
            engine=engine,
            store=store,
            ledger=ledger,
            crypto=crypto,
            pipeline=_pipeline(store, ledger, crypto, redis),
            settings=settings,
        )
    finally:
        if getattr(engine.sync_engine, "_sms_uncertain_audit_begin", False):
            event.remove(engine.sync_engine, "begin", _set_audit_transaction_context)
            engine.sync_engine._sms_uncertain_audit_begin = False
        await redis.aclose()
        await engine.dispose()


def _effect_actor(resolution: Any) -> UncertainEffectPrincipal:
    return UncertainEffectPrincipal(
        resolution.id,
        resolution.proposer_account_id,
        int(resolution.confirmer_account_id),
        resolution.effect_generation,
        str(resolution.source_dept),
    )


@pytest.mark.asyncio
async def test_web_unknown_resend_creates_system_subject_child(
    resend_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposer = await _create_admin(resend_env.engine, f"p-{uuid4().hex[:8]}")
    confirmer = await _create_admin(resend_env.engine, f"c-{uuid4().hex[:8]}")
    _batch_id, chunk_id = await _seed_unknown_batch(
        resend_env.engine,
        resend_env.crypto,
        channel="web",
        dept="运营一部",
        app_id=None,
    )
    service = BoundResolution(resend_env.crypto, resend_env.engine)

    async def fake_pipeline(_app: object) -> SendPipeline:
        return resend_env.pipeline

    monkeypatch.setattr("app.api.messages._pipeline", fake_pipeline)
    proposed = await service.propose(chunk_id, "resend_new_batch", proposer)
    confirmed = await service.confirm(proposed.id, confirmer)
    assert confirmed.source_dept == "运营一部"
    with audit_principal_scope(_effect_actor(confirmed)), correlation_scope(uuid4()):
        closed = await service.apply_effect(proposed.id)
    assert closed.state == "closed"
    async with resend_env.engine.connect() as connection:
        child = (
            await connection.execute(
                text(
                    """
                    SELECT b.id,b.app_id,b.dept,b.channel,r.subject_kind,r.app_id usage_app
                    FROM sms_uncertain_child c
                    JOIN sms_batch b ON b.id=c.child_batch_id
                    JOIN usage_reservation r ON r.id=b.usage_reservation_id
                    WHERE c.resolution_id=:id
                    """
                ),
                {"id": proposed.id},
            )
        ).mappings().one()
        system_app = int(
            (
                await connection.execute(
                    text("SELECT id FROM app WHERE name=:name"),
                    {"name": SYSTEM_UNCERTAIN_RESEND_APP_NAME},
                )
            ).scalar_one()
        )
    assert int(child["app_id"]) == system_app
    assert int(child["app_id"]) >= 1
    assert str(child["dept"]) == "运营一部"
    assert str(child["subject_kind"]) == "system_effect"
    assert int(child["usage_app"]) == system_app
    assert str(child["channel"]) == "web"
    with audit_principal_scope(_effect_actor(confirmed)), correlation_scope(uuid4()):
        again = await service.apply_effect(proposed.id)
    assert again.child_batch_id == int(child["id"])


@pytest.mark.asyncio
async def test_api_unknown_resend_uses_source_app(
    resend_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = uuid4().hex[:8]
    app_id = await _insert_api_app(resend_env.engine, nonce)
    proposer = await _create_admin(resend_env.engine, f"ap-{nonce}")
    confirmer = await _create_admin(resend_env.engine, f"ac-{nonce}")
    _batch_id, chunk_id = await _seed_unknown_batch(
        resend_env.engine,
        resend_env.crypto,
        channel="api",
        dept="平台部",
        app_id=app_id,
    )
    service = BoundResolution(resend_env.crypto, resend_env.engine)

    async def fake_pipeline(_app: object) -> SendPipeline:
        return resend_env.pipeline

    monkeypatch.setattr("app.api.messages._pipeline", fake_pipeline)
    proposed = await service.propose(chunk_id, "resend_new_batch", proposer)
    confirmed = await service.confirm(proposed.id, confirmer)
    with audit_principal_scope(_effect_actor(confirmed)), correlation_scope(uuid4()):
        closed = await service.apply_effect(proposed.id)
    assert closed.state == "closed"
    async with resend_env.engine.connect() as connection:
        child = (
            await connection.execute(
                text(
                    """
                    SELECT b.app_id,r.subject_kind,r.dept
                    FROM sms_uncertain_child c
                    JOIN sms_batch b ON b.id=c.child_batch_id
                    JOIN usage_reservation r ON r.id=b.usage_reservation_id
                    WHERE c.resolution_id=:id
                    """
                ),
                {"id": proposed.id},
            )
        ).mappings().one()
    assert int(child["app_id"]) == app_id
    assert str(child["subject_kind"]) == "api_app"
    assert str(child["dept"]) == "平台部"


@pytest.mark.asyncio
async def test_disabled_source_app_enters_manual_intervention(
    resend_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = uuid4().hex[:8]
    app_id = await _insert_api_app(resend_env.engine, nonce)
    proposer = await _create_admin(resend_env.engine, f"dp-{nonce}")
    confirmer = await _create_admin(resend_env.engine, f"dc-{nonce}")
    _batch_id, chunk_id = await _seed_unknown_batch(
        resend_env.engine,
        resend_env.crypto,
        channel="api",
        dept="平台部",
        app_id=app_id,
    )
    service = BoundResolution(resend_env.crypto, resend_env.engine)
    proposed = await service.propose(chunk_id, "resend_new_batch", proposer)
    await service.confirm(proposed.id, confirmer)
    async with resend_env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE app SET status=0 WHERE id=:id"),
            {"id": app_id},
        )

    async def fake_pipeline(_app: object) -> SendPipeline:
        raise AssertionError("disabled source app must not call pipeline")

    monkeypatch.setattr("app.api.messages._pipeline", fake_pipeline)
    with pytest.raises(Exception, match="源应用不可用"):
        await service.apply_effect(proposed.id)
    async with resend_env.engine.connect() as connection:
        state = (
            await connection.execute(
                text("SELECT state,effect_error FROM sms_uncertain_resolution WHERE id=:id"),
                {"id": proposed.id},
            )
        ).mappings().one()
    assert str(state["state"]) == "manual_intervention_required"
    assert str(state["effect_error"]) == "source_app_invalid"


@pytest.mark.asyncio
async def test_web_resolution_without_dept_stays_manual(resend_env: Any) -> None:
    nonce = uuid4().hex[:8]
    proposer = await _create_admin(resend_env.engine, f"wp-{nonce}")
    confirmer = await _create_admin(resend_env.engine, f"wc-{nonce}")
    _batch_id, chunk_id = await _seed_unknown_batch(
        resend_env.engine,
        resend_env.crypto,
        channel="web",
        dept="运营一部",
        app_id=None,
    )
    service = BoundResolution(resend_env.crypto, resend_env.engine)
    proposed = await service.propose(chunk_id, "resend_new_batch", proposer)
    async with resend_env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE sms_batch SET dept='' WHERE id=:id"),
            {"id": _batch_id},
        )
    with pytest.raises(Exception, match="source dept unavailable"):
        await service.confirm(proposed.id, confirmer)


@pytest.mark.asyncio
async def test_usage_ledger_rejects_negative_app_id(resend_env: Any) -> None:
    with pytest.raises(ValueError, match="invalid usage reservation"):
        await resend_env.ledger.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=-1,
            dept="运营一部",
            category="notice",
            subject_kind="system_effect",
        )


def test_api_key_lookup_excludes_system_effect_apps() -> None:
    sql = inspect.getsource(SqlApiKeyRepository.find_candidates)
    assert "usage_subject_kind = 'api_app'" in sql
