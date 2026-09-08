from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal, UncertainEffectPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.crypto import CryptoService, EncryptionContext
from app.services.idempotency import IdempotencyCoordinator
from app.services.pipeline import (
    AllFiltered,
    BatchResponse,
    InFlightLimitExceeded,
    PipelineConfig,
    SendPipeline,
    SensitiveWord,
)
from app.services.pipeline_repository import SqlPipelineStore
from app.services.quota import QuotaExceeded
from app.services.send_admission import SendAdmissionRejected
from app.services.uncertain_resolution import (
    UncertainResolutionConflict,
    UncertainResolutionService,
)
from app.services.usage_ledger import UsageLedgerService, UsageProjectionUnavailable
from app.services.usage_subject import SYSTEM_UNCERTAIN_RESEND_APP_NAME
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)

_AES = os.environ.get("TEST_DATA_AES_KEY") or __import__("base64").b64encode(b"v" * 32).decode()
_HMAC = os.environ.get("TEST_DATA_HMAC_KEY") or __import__("base64").b64encode(b"v" * 32).decode()


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(_AES, _HMAC)


def _phone(nonce: str, seed: int) -> str:
    return f"138{(int(nonce[:8], 16) + seed) % 10**8:08d}"


class EngineBoundStore(SqlPipelineStore):
    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(settings=settings, crypto=_crypto())
        self._bound_engine = engine
        sync_engine = engine.sync_engine
        if not getattr(sync_engine, "_sms_uncertain_audit_begin", False):
            event.listen(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_uncertain_audit_begin = True

    def _engine(self) -> Any:
        return self._bound_engine


class EngineBoundLedger(UsageLedgerService):
    def __init__(self, engine: Any, redis: Any, settings: Any) -> None:
        super().__init__(redis, settings)
        self._bound_engine = engine

    def _engine(self) -> Any:
        return self._bound_engine


class _NoDisposeEngine:
    """apply_effect / confirm 会 dispose；测试共享 engine 不能被关掉。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def begin(self) -> Any:
        return self._engine.begin()

    def connect(self) -> Any:
        return self._engine.connect()

    async def dispose(self) -> None:
        return None


class EngineBoundResolution(UncertainResolutionService):
    def __init__(self, engine: Any, crypto: CryptoService) -> None:
        super().__init__(crypto)
        self._bound_engine = _NoDisposeEngine(engine)

    def _engine(self) -> Any:
        return self._bound_engine


class PolicyStore(EngineBoundStore):
    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(engine, settings)
        self.blocked: set[str] = set()
        self.sensitive = False

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        return self.blocked & phone_hmacs

    async def sensitive_hits(self, content: str) -> list[str]:
        return ["违禁"] if self.sensitive else []


class FakeFrequency:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow_next = allow

    async def allow(self, *_args: object, **_kwargs: object) -> bool:
        return self.allow_next


class FakeQuota:
    async def reserve(self, **_values: object) -> None:
        return None

    async def refund(self, **_values: object) -> None:
        return None

    async def refund_reservation(self, **_values: object) -> None:
        return None


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.events.append((batch_no, queue))


class DenyAdmission:
    async def authorize(self, **_values: object) -> None:
        raise SendAdmissionRejected("closed", "test", 30)


class DenyFreqLedger(EngineBoundLedger):
    async def allow_frequency_many(self, *_args: object, **_kwargs: object) -> list[bool]:
        assert _args[1] in {"verify", "market"}
        items = _kwargs.get("items") or ()
        return [False] * len(tuple(items))


class FailRedis:
    async def get(self, *_args: object, **_kwargs: object) -> None:
        raise ConnectionError("synthetic redis outage")

    async def set(self, *_args: object, **_kwargs: object) -> bool:
        raise ConnectionError("synthetic redis outage")


async def _prepare_db(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _insert_admin(engine: Any, nonce: str, suffix: str) -> SecurityPrincipal:
    login = f"u633{suffix}{nonce[:8]}"
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
                          :account_id,:provider_id,:login,:login,:subject
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "login": login,
                        "subject": f"local:{login}",
                    },
                )
            ).scalar_one()
        )
    return SecurityPrincipal(account_id, identity_id, login, "平台部", "admin")


async def _system_app_id(engine: Any) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            text(
                """
                SELECT id FROM app
                WHERE name=:name AND usage_subject_kind='system_effect'
                """
            ),
            {"name": SYSTEM_UNCERTAIN_RESEND_APP_NAME},
        )
    if value is None:
        raise AssertionError("system-uncertain-resend app missing")
    app_id = int(value)
    if app_id < 1:
        raise AssertionError("system-uncertain-resend app_id must be positive")
    return app_id


async def _insert_api_app(engine: Any, nonce: str, *, categories: str = "notice") -> int:
    async with engine.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,
                          allowed_categories,daily_quota,created_by
                        ) VALUES(
                          :name,'研发部',:hash,:prefix,:categories,1000,'test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"src-{nonce}",
                        "hash": "b" * 64,
                        "prefix": nonce[:8],
                        "categories": categories,
                    },
                )
            ).scalar_one()
        )


async def _insert_unknown(
    engine: Any,
    crypto: CryptoService,
    *,
    channel: str,
    dept: str,
    app_id: int | None,
    phone: str,
    category: str = "notice",
    content: str = "人工重发通知",
) -> tuple[int, int, int]:
    batch_no = uuid4().hex
    custom_id = uuid4().hex
    display = crypto.encrypt_bound_packed_text(
        content,
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=batch_no,
        ),
    )
    send = crypto.encrypt_bound_packed_text(
        content,
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id=batch_no,
        ),
    )
    protected = crypto.protect_phone(phone)
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
                          :batch_no,:category,:channel,:app_id,'integration',:dept,
                          '[encrypted]',:display,:send,1,1,'completed_unknown',1,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "category": category,
                        "channel": channel,
                        "app_id": app_id,
                        "dept": dept,
                        "display": display,
                        "send": send,
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
                          batch_id,chunk_no,custom_id,phone_count,status,
                          unknown_terminal_at
                        ) VALUES(
                          :batch_id,1,:custom_id,1,'unknown_terminal',now()
                        ) RETURNING id
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
                  :batch_id,:chunk_id,:enc,:hmac,:mask,:version,'unknown'
                )
                """
            ),
            {
                "batch_id": batch_id,
                "chunk_id": chunk_id,
                "enc": protected.phone_enc,
                "hmac": protected.phone_hmac,
                "mask": protected.phone_mask,
                "version": protected.key_version,
            },
        )
    return batch_id, chunk_id


def _pipeline(
    store: SqlPipelineStore,
    ledger: UsageLedgerService | None,
    redis: Any,
    *,
    admission: Any = None,
    frequency: Any | None = None,
) -> SendPipeline:
    return SendPipeline(
        store=store,
        idempotency=IdempotencyCoordinator(redis, store),
        crypto=_crypto(),
        frequency=frequency or FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        admission_guard=admission,
        config=PipelineConfig(),
    )


async def _approve(
    service: UncertainResolutionService,
    chunk_id: int,
    proposer: SecurityPrincipal,
    confirmer: SecurityPrincipal,
) -> int:
    proposed = await service.propose(chunk_id, "resend_new_batch", proposer)
    confirmed = await service.confirm(proposed.id, confirmer)
    assert confirmed.source_dept not in {None, "", "web"} or confirmed.source_channel == "web"
    return confirmed.id


@pytest_asyncio.fixture
async def env() -> Any:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    await _prepare_db(engine)
    from redis.asyncio import Redis

    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            redis_control_url=os.environ["AUTH_GUARD_REDIS_URL"],
            vendor_mock=True,
        ),
    )
    store = PolicyStore(engine, settings)
    ledger = EngineBoundLedger(engine, redis, settings)
    crypto = _crypto()
    service = EngineBoundResolution(engine, crypto)
    nonce = uuid4().hex
    proposer = await _insert_admin(engine, nonce, "a")
    confirmer = await _insert_admin(engine, nonce, "b")
    system_app_id = await _system_app_id(engine)
    try:
        yield SimpleNamespace(
            engine=engine,
            redis=redis,
            settings=settings,
            store=store,
            ledger=ledger,
            service=service,
            proposer=proposer,
            confirmer=confirmer,
            system_app_id=system_app_id,
            nonce=nonce,
        )
    finally:
        if getattr(engine.sync_engine, "_sms_uncertain_audit_begin", False):
            event.remove(engine.sync_engine, "begin", _set_audit_transaction_context)
            engine.sync_engine._sms_uncertain_audit_begin = False
        await redis.aclose()
        await engine.dispose()


async def _apply(
    env: Any,
    resolution_id: int,
    pipeline: SendPipeline,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    async def fake_pipeline(_app: ApiAppContext) -> SendPipeline:
        return pipeline

    monkeypatch.setattr("app.api.messages._pipeline", fake_pipeline)
    principal = UncertainEffectPrincipal(
        resolution_id,
        env.proposer.account_id,
        env.confirmer.account_id,
        1,
        "运营一部",
    )
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        return await env.service.apply_effect(resolution_id)


@pytest.mark.asyncio
async def test_start_reservation_rejects_negative_and_zero_system(
    env: Any,
) -> None:
    with pytest.raises(ValueError, match="invalid usage reservation"):
        await env.ledger.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=-1,
            dept="运营一部",
            category="notice",
        )
    with pytest.raises(ValueError, match="invalid usage reservation"):
        await env.ledger.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=0,
            dept="运营一部",
            category="notice",
            subject_kind="system_effect",
        )
    created = await env.ledger.start_reservation(
        request_key=f"acceptance:{uuid4()}",
        app_id=env.system_app_id,
        dept="运营一部",
        category="notice",
        subject_kind="system_effect",
    )
    assert created.reservation_id is not None


@pytest.mark.asyncio
async def test_web_unknown_dual_control_creates_child(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 13),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    result = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert result.state == "closed"
    async with env.engine.connect() as connection:
        child = (
            await connection.execute(
                text(
                    """
                    SELECT b.id,b.dept,b.app_id,b.channel,r.subject_kind,r.app_id usage_app
                    FROM sms_uncertain_child c
                    JOIN sms_batch b ON b.id=c.child_batch_id
                    JOIN sms_batch src ON src.id=:source_id
                    LEFT JOIN usage_reservation r ON r.id=b.usage_reservation_id
                    WHERE c.resolution_id=:id
                    """
                ),
                {"id": resolution_id, "source_id": batch_id},
            )
        ).mappings().one()
        original = (
            await connection.execute(
                text(
                    """
                    SELECT b.status batch_status,c.status chunk_status,m.status msg_status
                    FROM sms_batch b
                    JOIN sms_chunk c ON c.batch_id=b.id
                    JOIN sms_message m ON m.chunk_id=c.id
                    WHERE b.id=:id
                    """
                ),
                {"id": batch_id},
            )
        ).mappings().one()
    assert int(child["app_id"]) == env.system_app_id
    assert child["dept"] == "运营一部"
    assert child["subject_kind"] == "system_effect"
    assert int(child["usage_app"]) == env.system_app_id
    assert int(child["usage_app"]) != -1
    assert original["batch_status"] == "completed_unknown"
    assert original["chunk_status"] == "unknown_terminal"
    assert original["msg_status"] == "unknown"


@pytest.mark.asyncio
async def test_api_unknown_uses_source_app(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = await _insert_api_app(env.engine, env.nonce)
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="api",
        dept="研发部",
        app_id=app_id,
        phone=_phone(env.nonce, 14),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    result = await _apply(
        env,
        resolution_id,
        _pipeline(env.store, env.ledger, env.redis),
        monkeypatch,
    )
    assert result.state == "closed"
    async with env.engine.connect() as connection:
        child = (
            await connection.execute(
                text(
                    """
                    SELECT b.app_id,b.dept,r.subject_kind,r.app_id usage_app
                    FROM sms_uncertain_child c
                    JOIN sms_batch b ON b.id=c.child_batch_id
                    JOIN usage_reservation r ON r.id=b.usage_reservation_id
                    WHERE c.resolution_id=:id
                    """
                ),
                {"id": resolution_id},
            )
        ).mappings().one()
    assert int(child["app_id"]) == app_id
    assert child["dept"] == "研发部"
    assert child["subject_kind"] == "api_app"
    assert int(child["usage_app"]) == app_id


@pytest.mark.asyncio
async def test_disabled_source_app_is_manual(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = await _insert_api_app(env.engine, env.nonce + "d")
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="api",
        dept="研发部",
        app_id=app_id,
        phone=_phone(env.nonce, 15),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    async with env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE app SET status=0 WHERE id=:id"),
            {"id": app_id},
        )
    with pytest.raises(UncertainResolutionConflict, match="源应用不可用"):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state = await connection.scalar(
            text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
            {"id": resolution_id},
        )
    assert state == "manual_intervention_required"


@pytest.mark.asyncio
async def test_revoked_category_is_manual(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = await _insert_api_app(env.engine, env.nonce + "c", categories="notice")
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="api",
        dept="研发部",
        app_id=app_id,
        phone=_phone(env.nonce, 16),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    async with env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE app SET allowed_categories='verify' WHERE id=:id"),
            {"id": app_id},
        )
    with pytest.raises(UncertainResolutionConflict, match="类别"):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state, error = (
            await connection.execute(
                text(
                    "SELECT state,effect_error FROM sms_uncertain_resolution WHERE id=:id"
                ),
                {"id": resolution_id},
            )
        ).one()
    assert state == "manual_intervention_required"
    assert error in {"source_category_invalid", "source_context_invalid"}


@pytest.mark.asyncio
async def test_system_quota_exceeded_rejects(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with env.engine.begin() as connection:
        previous = await connection.scalar(
            text("SELECT daily_quota FROM app WHERE id=:id"),
            {"id": env.system_app_id},
        )
        used = await connection.scalar(
            text(
                """
                SELECT COALESCE(sum(quota_cost),0)
                FROM usage_reservation
                WHERE app_id=:id
                  AND usage_date=((now() AT TIME ZONE 'Asia/Shanghai')::date)
                  AND state IN ('reserved','committed','uncertain')
                """
            ),
            {"id": env.system_app_id},
        )
        await connection.execute(
            text("UPDATE app SET daily_quota=:quota WHERE id=:id"),
            {"id": env.system_app_id, "quota": int(used or 0) + 1},
        )
    try:
        _batch_id, chunk_id = await _insert_unknown(
            env.engine,
            _crypto(),
            channel="web",
            dept="运营一部",
            app_id=None,
            phone=_phone(env.nonce, 17),
        )
        first = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
        await _apply(env, first, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
        _batch_id2, chunk_id2 = await _insert_unknown(
            env.engine,
            _crypto(),
            channel="web",
            dept="运营一部",
            app_id=None,
            phone=_phone(env.nonce, 18),
        )
        second = await _approve(env.service, chunk_id2, env.proposer, env.confirmer)
        with pytest.raises(QuotaExceeded):
            await _apply(env, second, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    finally:
        async with env.engine.begin() as connection:
            await connection.execute(
                text("UPDATE app SET daily_quota=:quota WHERE id=:id"),
                {"id": env.system_app_id, "quota": previous},
            )


@pytest.mark.asyncio
async def test_blacklist_sensitive_admission_inflight_reject(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = _phone(env.nonce, 19)
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=phone,
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    hmac = _crypto().protect_phone(phone).phone_hmac
    env.store.blocked = {hmac}
    with pytest.raises(AllFiltered):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state = await connection.scalar(
            text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
            {"id": resolution_id},
        )
    assert state == "manual_intervention_required"

    env.store.blocked = set()
    env.store.sensitive = True
    _batch_id2, chunk_id2 = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 20),
    )
    second = await _approve(env.service, chunk_id2, env.proposer, env.confirmer)
    with pytest.raises(SensitiveWord):
        await _apply(env, second, _pipeline(env.store, env.ledger, env.redis), monkeypatch)

    env.store.sensitive = False
    _batch_freq, chunk_freq = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 31),
        category="verify",  # notice 不适用号码频控；用真实受控类别验证频控阻断。
    )
    freq_id = await _approve(env.service, chunk_freq, env.proposer, env.confirmer)
    with pytest.raises(AllFiltered):
        await _apply(
            env,
            freq_id,
            _pipeline(
                env.store,
                DenyFreqLedger(env.engine, env.redis, env.settings),
                env.redis,
            ),
            monkeypatch,
        )

    _batch_id3, chunk_id3 = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 21),
    )
    third = await _approve(env.service, chunk_id3, env.proposer, env.confirmer)
    with pytest.raises(SendAdmissionRejected):
        await _apply(
            env,
            third,
            _pipeline(env.store, env.ledger, env.redis, admission=DenyAdmission()),
            monkeypatch,
        )

    _batch_id4, chunk_id4 = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 22),
    )
    fourth = await _approve(env.service, chunk_id4, env.proposer, env.confirmer)
    async with env.engine.connect() as connection:
        current = int(
            await connection.scalar(
                text(
                    "SELECT COALESCE(reserved_chunks,0) FROM send_inflight_balance "
                    "WHERE app_id=:id"
                ),
                {"id": env.system_app_id},
            )
            or 0
        )
    reserved = None
    remaining = max(0, 200 - current)
    if remaining:
        reserved = await env.store.reserve_in_flight_chunks(
            env.system_app_id, remaining, 200
        )
    try:
        with pytest.raises(InFlightLimitExceeded):
            await _apply(env, fourth, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    finally:
        if reserved is not None:
            await env.store.release_unbound_acceptance_reservation(
                reserved.id,
                reserved.generation,
                env.system_app_id,
            )


@pytest.mark.asyncio
async def test_worker_crash_recovers_same_child(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 23),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    async with env.engine.begin() as connection:
        from app.services.uncertain_resolution import _load_resend_context, _row

        current = (
            await connection.execute(
                text(
                    """
                    SELECT id,chunk_id,batch_id,action,state,
                      proposer_account_id,confirmer_account_id,child_batch_id,
                      effect_generation,effect_error,source_app_id,source_channel,
                      source_category,source_dept
                    FROM sms_uncertain_resolution WHERE id=:id
                    """
                ),
                {"id": resolution_id},
            )
        ).mappings().one()
        context, app_ctx = await _load_resend_context(connection, _row(current))
        request = await env.service._build_resend(
            connection,
            chunk_id=chunk_id,
            resolution_id=resolution_id,
            generation=1,
            actor=UncertainEffectPrincipal(
                resolution_id,
                env.proposer.account_id,
                env.confirmer.account_id,
                1,
                "运营一部",
            ),
            usage_subject=context.usage_subject,
        )
    with audit_principal_scope(request.actor), correlation_scope(uuid4()):
        accepted = await pipeline.accept(app_ctx, request)
    assert isinstance(accepted, BatchResponse)
    recovered = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert recovered.state == "closed"
    async with env.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT child_batch_id,recovered,generation
                    FROM sms_uncertain_child WHERE resolution_id=:id
                    """
                ),
                {"id": resolution_id},
            )
        ).mappings().all()
        usage = await connection.scalar(
            text(
                """
                SELECT count(*) FROM usage_reservation r
                JOIN sms_batch b ON b.usage_reservation_id=r.id
                JOIN sms_uncertain_child c ON c.child_batch_id=b.id
                WHERE c.resolution_id=:id
                """
            ),
            {"id": resolution_id},
        )
    assert len(rows) == 1
    assert rows[0]["recovered"] is False  # 关系已与创建事务原子提交
    assert int(usage) == 1


@pytest.mark.asyncio
async def test_concurrent_effect_workers_one_child(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 24),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)

    async def run() -> None:
        try:
            await _apply(env, resolution_id, pipeline, monkeypatch)
        except UncertainResolutionConflict:
            return

    await asyncio.gather(run(), run())
    async with env.engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id"),
            {"id": resolution_id},
        )
        batches = await connection.scalar(
            text(
                """
                SELECT count(*) FROM sms_batch
                WHERE biz_id=:biz
                """
            ),
            {"biz": f"manual-resend:{resolution_id}:1"},
        )
    assert int(count) == 1
    assert int(batches) == 1


@pytest.mark.asyncio
async def test_generation_mismatch_is_manual(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 25),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE sms_uncertain_resolution
                SET state='effect_pending', effect_generation=2, child_batch_id=NULL
                WHERE id=:id
                """
            ),
            {"id": resolution_id},
        )
    with pytest.raises(UncertainResolutionConflict, match="generation"):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state = await connection.scalar(
            text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
            {"id": resolution_id},
        )
    assert state == "manual_intervention_required"


@pytest.mark.asyncio
async def test_disabled_confirmer_is_manual(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 26),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    async with env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE user_account SET status=0 WHERE id=:id"),
            {"id": env.confirmer.account_id},
        )
    with pytest.raises(UncertainResolutionConflict, match="失效"):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state = await connection.scalar(
            text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
            {"id": resolution_id},
        )
    assert state == "manual_intervention_required"


@pytest.mark.asyncio
async def test_stock_web_resolution_without_dept_is_isolated(env: Any) -> None:
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 27),
    )
    async with env.engine.begin() as connection:
        resolution_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_uncertain_resolution(
                          chunk_id,batch_id,action,state,proposer_account_id,
                          confirmer_account_id,confirmed_at,approved_at,
                          source_channel,source_category
                        ) VALUES(
                          :chunk_id,:batch_id,'resend_new_batch','effect_pending',
                          :proposer,:confirmer,now(),now(),'web','notice'
                        ) RETURNING id
                        """
                    ),
                    {
                        "chunk_id": chunk_id,
                        "batch_id": _batch_id,
                        "proposer": env.proposer.account_id,
                        "confirmer": env.confirmer.account_id,
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                UPDATE sms_uncertain_resolution
                SET state='manual_intervention_required',
                    effect_error='source_context_invalid'
                WHERE id=:id
                  AND COALESCE(source_channel,'')='web'
                  AND (source_dept IS NULL OR btrim(source_dept)='')
                """
            ),
            {"id": resolution_id},
        )
        state, error, dept = (
            await connection.execute(
                text(
                    """
                    SELECT state,effect_error,source_dept
                    FROM sms_uncertain_resolution WHERE id=:id
                    """
                ),
                {"id": resolution_id},
            )
        ).one()
    assert state == "manual_intervention_required"
    assert error == "source_context_invalid"
    assert dept is None


@pytest.mark.asyncio
async def test_usage_ledger_outage_is_retryable_then_recovers(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 28),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    broken = EngineBoundLedger(env.engine, FailRedis(), env.settings)
    with pytest.raises((UsageProjectionUnavailable, ConnectionError)):
        await _apply(env, resolution_id, _pipeline(env.store, broken, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        state = await connection.scalar(
            text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
            {"id": resolution_id},
        )
    assert state == "retryable_effect_error"
    recovered = await _apply(
        env,
        resolution_id,
        _pipeline(env.store, env.ledger, env.redis),
        monkeypatch,
    )
    assert recovered.state == "closed"


@pytest.mark.asyncio
async def test_late_evidence_and_callback_preserved(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = await _insert_api_app(env.engine, env.nonce + "cb")
    batch_id, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="api",
        dept="研发部",
        app_id=app_id,
        phone=_phone(env.nonce, 29),
    )
    async with env.engine.begin() as connection:
        await connection.execute(
            text("UPDATE sms_chunk SET late_evidence_at=now() WHERE id=:id"),
            {"id": chunk_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO callback_task(
                  app_id,event,batch_id,url,callback_secret_enc,
                  callback_secret_key_version,status
                ) VALUES(
                  :app_id,'batch.finished',:batch_id,'http://127.0.0.1/cb',
                  :secret,1,'pending'
                )
                """
            ),
            {"app_id": app_id, "batch_id": batch_id, "secret": b"secret"},
        )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        chunk = (
            await connection.execute(
                text(
                    """
                    SELECT status,late_evidence_at IS NOT NULL late
                    FROM sms_chunk WHERE id=:id
                    """
                ),
                {"id": chunk_id},
            )
        ).mappings().one()
        callbacks = await connection.scalar(
            text(
                """
                SELECT count(*) FROM callback_task
                WHERE batch_id=:id AND status='pending'
                """
            ),
            {"id": batch_id},
        )
    assert chunk["status"] == "unknown_terminal"
    assert chunk["late"] is True
    assert int(callbacks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_app", [True, False])
async def test_ordinary_legacy_named_candidate_is_not_claimed(
    env: Any, monkeypatch: pytest.MonkeyPatch, same_app: bool
) -> None:
    from app.services.pipeline import SendRequest

    source_app = await _insert_api_app(env.engine, env.nonce)
    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="api",
        dept="运营一部",
        app_id=source_app,
        phone=_phone(env.nonce, 100),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    app_id = source_app if same_app else await _insert_api_app(env.engine, env.nonce + "other")
    app = ApiAppContext(
        app_id,
        f"system_resend:{resolution_id}",
        "运营一部",
        frozenset({"notice"}),
        daily_quota=1000,
    )
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    # 实际普通 Pipeline 创建；仅夹具把幂等记录移入旧版本可污染的命名空间。
    from app.core.auth.accounts import ApplicationPrincipal

    with (
        audit_principal_scope(ApplicationPrincipal(app.app_id, app.name, app.dept)),
        correlation_scope(uuid4()),
    ):
        result = await pipeline.accept(
            app,
            SendRequest(
                "notice",
                (_phone(env.nonce, 101),),
                content="普通通知",
                biz_id=f"manual-resend:{resolution_id}:1",
            ),
        )
    async with env.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE idempotency_record SET scope_kind='uncertain-resend',scope_id=:scope "
                "WHERE biz_id=:biz AND app_id=:app"
            ),
            {"scope": str(resolution_id), "biz": f"manual-resend:{resolution_id}:1", "app": app_id},
        )
    with pytest.raises(UncertainResolutionConflict):
        await _apply(env, resolution_id, pipeline, monkeypatch)
    async with env.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id"),
                {"id": resolution_id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT state FROM sms_uncertain_resolution WHERE id=:id"),
                {"id": resolution_id},
            )
            == "manual_intervention_required"
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_batch WHERE batch_no=:no"), {"no": result.batch_no}
            )
            == 1
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_batch WHERE biz_id=:biz"),
                {"biz": f"manual-resend:{resolution_id}:1"},
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "after"])
async def test_child_relation_failure_rolls_back_entire_acceptance(
    env: Any, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    import app.services.uncertain_resolution as resolution_module

    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 102),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    original = resolution_module.bind_uncertain_child

    async def fail(*args: Any, **kwargs: Any) -> None:
        if when == "after":
            await original(*args, **kwargs)
        raise RuntimeError("synthetic relation failure")

    monkeypatch.setattr(resolution_module, "bind_uncertain_child", fail)
    with pytest.raises(RuntimeError, match="synthetic relation failure"):
        await _apply(env, resolution_id, _pipeline(env.store, env.ledger, env.redis), monkeypatch)
    async with env.engine.connect() as connection:
        params = {
            "id": resolution_id,
            "scope": str(resolution_id),
            "biz": f"manual-resend:{resolution_id}:1",
        }
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id"), params
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_batch WHERE biz_id=:biz"), params
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM idempotency_record WHERE scope_kind='uncertain-resend' "
                    "AND scope_id=:scope"
                ),
                params,
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM idempotency_claim WHERE scope_kind='uncertain-resend' "
                    "AND scope_id=:scope AND (state='completed' OR batch_id IS NOT NULL)"
                ),
                params,
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT child_batch_id FROM sms_uncertain_resolution WHERE id=:id"), params
            )
            is None
        )


@pytest.mark.asyncio
async def test_proven_legacy_child_can_recover_using_system_audit_and_fingerprint(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 103),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    closed = await _apply(env, resolution_id, pipeline, monkeypatch)
    async with env.engine.begin() as connection:
        # 保留不可变审计和完整指纹，模拟旧版本 COMMIT 后、写关系前崩溃。
        await connection.execute(
            text("DELETE FROM sms_uncertain_child WHERE resolution_id=:id"), {"id": resolution_id}
        )
        await connection.execute(
            text(
                "UPDATE sms_uncertain_resolution SET state='effect_pending',child_batch_id=NULL "
                "WHERE id=:id"
            ),
            {"id": resolution_id},
        )
    recovered = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert recovered.child_batch_id == closed.child_batch_id
    async with env.engine.connect() as connection:
        assert (
            await connection.execute(
                text(
                    "SELECT recovered,provenance_verified FROM sms_uncertain_child WHERE "
                    "resolution_id=:id"
                ),
                {"id": resolution_id},
            )
        ).one() == (True, True)
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_batch WHERE biz_id=:biz"),
                {"biz": f"manual-resend:{resolution_id}:1"},
            )
            == 1
        )


@pytest.mark.asyncio
async def test_effect_lost_accept_response_recovers_atomic_child(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 104),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    accept = pipeline.accept
    calls = 0

    async def lost_response(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await accept(*args, **kwargs)
        raise ConnectionError("synthetic response lost after commit")

    pipeline.accept = lost_response
    with pytest.raises(ConnectionError):
        await _apply(env, resolution_id, pipeline, monkeypatch)
    closed = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert closed.state == "closed" and closed.child_batch_id is not None
    assert calls == 1
    async with env.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM sms_batch WHERE biz_id=:biz"),
                {"biz": f"manual-resend:{resolution_id}:1"},
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_effect_uses_send_runtime_permissions(
    env: Any, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 105),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    child_id = None
    if legacy:
        child_id = (await _apply(env, resolution_id, pipeline, monkeypatch)).child_batch_id
        async with env.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sms_uncertain_child WHERE resolution_id=:id"),
                {"id": resolution_id},
            )
            await connection.execute(
                text(
                    "UPDATE sms_uncertain_resolution SET "
                    "state='effect_pending',child_batch_id=NULL "
                    "WHERE id=:id"
                ),
                {"id": resolution_id},
            )

    runtime = await _send_runtime(env, monkeypatch)
    try:
        result = await _apply_as_send(runtime, resolution_id, monkeypatch)
        assert result.state == "closed"
        if legacy:
            assert result.child_batch_id == child_id
        async with runtime.engine.connect() as connection:
            assert (await connection.execute(text("SELECT current_user,session_user"))).one() == (
                "sms_send",
                "sms_send",
            )
            assert not await connection.scalar(
                text("SELECT has_table_privilege(current_user,'audit_log','SELECT')")
            )
            for privilege in ("DELETE", "TRUNCATE"):
                assert not await connection.scalar(
                    text("SELECT has_table_privilege(current_user,'idempotency_claim',:privilege)"),
                    {"privilege": privilege},
                )
        assert set(runtime.key_reads) <= {"audit_system_realtime_context_key"}
        if not legacy:
            assert runtime.key_reads
    finally:
        await runtime.engine.dispose()


async def _send_runtime(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """只在官方一次性库创建合成登录；不补业务 GRANT 或覆盖待测迁移函数。"""
    from app.settings import get_settings

    password = uuid4().hex
    key = bytes.fromhex("35" * 32)
    async with env.engine.begin() as connection:
        await connection.execute(text(f"ALTER ROLE sms_send WITH LOGIN PASSWORD '{password}'"))
        await connection.execute(
            text(
                "INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at) "
                "VALUES ('system:realtime',:key,now()) ON CONFLICT (key_kind) DO UPDATE "
                "SET key_material=EXCLUDED.key_material,updated_at=now()"
            ),
            {"key": key},
        )
    settings = get_settings().model_copy(update={"audit_producer_domain": "realtime"})
    monkeypatch.setattr("app.settings.get_settings", lambda: settings)
    key_reads = []

    def read_key(name: str) -> bytes:
        key_reads.append(name)
        assert name == "audit_system_realtime_context_key"
        return key

    monkeypatch.setattr("app.core.runtime_resources._audit_context_key", read_key)
    url = env.settings.database_url.set(username="sms_send", password=password)
    engine = create_async_engine(url, hide_parameters=True)
    store_settings = SimpleNamespace(
        database_url=url, redis_control_url=env.settings.redis_control_url, vendor_mock=True
    )
    store = PolicyStore(engine, store_settings)
    ledger = EngineBoundLedger(engine, env.redis, store_settings)
    return SimpleNamespace(
        engine=engine,
        service=EngineBoundResolution(engine, _crypto()),
        pipeline=_pipeline(store, ledger, env.redis),
        key_reads=key_reads,
    )


async def _apply_as_send(runtime: Any, resolution_id: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def pipeline(_app: ApiAppContext) -> SendPipeline:
        return runtime.pipeline

    monkeypatch.setattr("app.api.messages._pipeline", pipeline)
    # 实际 Outbox worker 没有人类请求上下文，系统审计在写入事务单独绑定。
    with audit_principal_scope(), correlation_scope(uuid4()):
        return await runtime.service.apply_effect(resolution_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["actor", "generation", "marker", "relation", "provenance", "closed", "signature"]
)
async def test_send_audit_rejects_unproven_internal_creation(
    env: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from sqlalchemy.exc import DBAPIError

    _source, chunk_id = await _insert_unknown(
        env.engine,
        _crypto(),
        channel="web",
        dept="运营一部",
        app_id=None,
        phone=_phone(env.nonce, 106),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    runtime = await _send_runtime(env, monkeypatch)
    try:
        result = await _apply_as_send(runtime, resolution_id, monkeypatch)
        async with env.engine.begin() as connection:
            batch_no = await connection.scalar(
                text("SELECT trim(batch_no) FROM sms_batch WHERE id=:id"),
                {"id": result.child_batch_id},
            )
            if change != "closed":
                await connection.execute(
                    text("UPDATE sms_uncertain_resolution SET state='applying' WHERE id=:id"),
                    {"id": resolution_id},
                )
            if change == "relation":
                await connection.execute(
                    text("DELETE FROM sms_uncertain_child WHERE resolution_id=:id"),
                    {"id": resolution_id},
                )
            if change == "provenance":
                await connection.execute(
                    text(
                        "UPDATE sms_uncertain_child SET provenance_verified=false "
                        "WHERE resolution_id=:id"
                    ),
                    {"id": resolution_id},
                )
        actor = f"system_resend:{resolution_id + int(change == 'actor')}"
        marker = change != "marker"
        generation = 2 if change == "generation" else 1
        with (
            pytest.raises(DBAPIError, match="system audit|signature"),
            audit_principal_scope(),
            correlation_scope(uuid4()),
        ):
            async with runtime.engine.begin() as connection:
                await bind_connection_system_audit(
                    connection, actor_name=actor, action="message_send"
                )
                if change == "signature":
                    await connection.execute(
                        text("SELECT set_config('sms.audit_context_signature','',TRUE)")
                    )
                await connection.execute(
                    text(
                        "INSERT INTO audit_log"
                        "(actor,actor_subject_kind,action,object_type,object_id,after_val) "
                        "VALUES (:actor,'system','message_send','batch',"
                        ":batch_no,CAST(:after AS jsonb))"
                    ),
                    {
                        "actor": actor,
                        "batch_no": batch_no,
                        "after": json.dumps(
                            {"uncertain_resend": marker, "effect_generation": generation}
                        ),
                    },
                )
    finally:
        await runtime.engine.dispose()


@pytest.mark.asyncio
async def test_r8_source_policy_matches_auth_and_worker_cannot_read_credentials(env: Any) -> None:
    from app.core.apikey import SqlApiKeyRepository
    from app.services.uncertain_resolution import _load_source_api_app

    app_id = await _insert_api_app(env.engine, env.nonce, categories="verify,market")
    async with env.engine.begin() as connection:
        await connection.execute(text("""
            UPDATE app SET recipient_limit_per_min=2,segment_limit_per_min=3,
              freq_override=CAST(:override AS jsonb),allow_market_api_bulk=true
            WHERE id=:id
        """), {"id": app_id, "override": json.dumps({"verify_per_minute": 2})})
        await connection.execute(text("SET LOCAL ROLE sms_send"))
        context = await _load_source_api_app(connection, app_id, "verify")
        for column in ("api_key_hash", "api_key_prev_hash", "api_key_hash_version"):
            assert not await connection.scalar(text(
                "SELECT has_column_privilege(current_user,'app',:column,'SELECT')"
            ), {"column": column})
    repository = SqlApiKeyRepository()
    repository._engine = lambda: env.engine
    candidates = await repository.find_candidates(env.nonce[:8])
    candidate = next(item for item in candidates if item.app_id == app_id)
    assert context.recipient_limit_per_min == candidate.recipient_limit_per_min == 2
    assert context.segment_limit_per_min == candidate.segment_limit_per_min == 3
    assert context.freq_override == candidate.freq_override == {"verify_per_minute": 2}
    assert context.allow_market_api_bulk is candidate.allow_market_api_bulk is True


@pytest.mark.asyncio
async def test_r8_resend_uses_current_cost_policy_and_recovers_same_child(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = await _insert_api_app(env.engine, env.nonce)
    _, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="api", dept="研发部", app_id=app_id,
        phone=_phone(env.nonce, 92), content="通" * 70,
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    async with env.engine.begin() as connection:
        await connection.execute(text("""
            UPDATE app SET recipient_limit_per_min=2,segment_limit_per_min=3,
              default_sign='后加签名' WHERE id=:id
        """), {"id": app_id})

    class Limiter:
        def __init__(self) -> None:
            self.costs: list[dict[str, Any]] = []

        async def check(self, **values: Any) -> None:
            pass

        async def check_replay(self, **values: Any) -> None:
            pass

        async def consume_send_cost(self, **values: Any) -> None:
            self.costs.append(values)

    limiter = Limiter()
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    pipeline.acceptance_limiter = limiter
    first = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert first.state == "closed"
    assert len(limiter.costs) == 1
    async with env.engine.connect() as connection:
        child = (await connection.execute(text(
            "SELECT sign_name,segments FROM sms_batch WHERE id=:id"
        ), {"id": first.child_batch_id})).mappings().one()
        assert child["sign_name"] is None and child["segments"] == 1
    assert limiter.costs[0]["recipient_limit"] == 2
    assert limiter.costs[0]["segment_limit"] == 3
    second = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert second.child_batch_id == first.child_batch_id
    assert len(limiter.costs) == 1


async def _r9_report(env: Any, chunk_id: int, *, status: int = 1) -> None:
    """通过真实报告仓储提交合成回执，不调用厂商。"""
    from datetime import UTC, datetime

    from app.services.report_ingest import ProtectedReport
    from app.services.report_repository import SqlReportRepository

    class Repository(SqlReportRepository):
        def _engine(self) -> Any:
            return _NoDisposeEngine(env.engine)

    async with env.engine.begin() as connection:
        row = (await connection.execute(text("""
            SELECT m.*,trim(c.custom_id) custom_id FROM sms_message m
            JOIN sms_chunk c ON c.id=m.chunk_id WHERE c.id=:id ORDER BY m.id LIMIT 1
        """), {"id": chunk_id})).mappings().one()
        raw_id = await connection.scalar(text("""
            INSERT INTO raw_vendor_log(source,payload_enc,payload_sha256,key_version,
              custom_ids,item_count) VALUES('report',:enc,:digest,1,:ids,1) RETURNING id
        """), {"enc": _crypto().encrypt_bound_packed_text('{}', EncryptionContext(
            domain="vendor-raw", table="raw_vendor_log",
            column="payload_enc", object_id="synthetic",
        )), "digest": uuid4().hex * 2,
                "ids": [row["custom_id"]]})
    report = ProtectedReport(
        event_key=uuid4().hex * 2, vendor_task_id="b" * 64, custom_id="c" * 64,
        match_custom_id=row["custom_id"], phone_enc=bytes(row["phone_enc"]),
        phone_hmac=str(row["phone_hmac"]).strip(), phone_mask=row["phone_mask"],
        key_version=int(row["key_version"]), report_status=status,
        message_status="delivered" if status == 1 else "unknown", report_desc="synthetic",
        report_time=datetime.now(UTC), phone_hmacs=(str(row["phone_hmac"]).strip(),),
    )
    result = await Repository(settings=env.settings).apply_report(int(raw_id), report)
    assert result is not None and result.changed


@pytest.mark.asyncio
async def test_r9_report_before_child_save_rejects_stale_snapshot(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="web", dept="运营一部", app_id=None,
        phone=_phone(env.nonce, 90),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    accept = pipeline.accept

    async def report_then_accept(app: Any, request: Any, **kwargs: Any) -> Any:
        await _r9_report(env, chunk_id)
        return await accept(app, request, **kwargs)

    monkeypatch.setattr(pipeline, "accept", report_then_accept)
    with pytest.raises(UncertainResolutionConflict, match="source_message_state_changed"):
        await _apply(env, resolution_id, pipeline, monkeypatch)
    async with env.engine.connect() as connection:
        resolution = (await connection.execute(text(
            "SELECT state,effect_error,child_batch_id FROM sms_uncertain_resolution WHERE id=:id"
        ), {"id": resolution_id})).mappings().one()
        assert dict(resolution) == {
            "state": "manual_intervention_required", "effect_error": "source_message_state_changed",
            "child_batch_id": None,
        }
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id"
        ), {"id": resolution_id}) == 0
        assert await connection.scalar(text(
            "SELECT status FROM sms_message WHERE batch_id=:id"
        ), {"id": batch_id}) == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "missing_proof", "signature", "identity", "created_at", "generation", "recipient",
    "deleted", "moved", "report_unknown",
])
async def test_r9_source_changes_roll_back_and_release_only_unbound(
    env: Any, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    batch_id, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="web", dept="运营一部", app_id=None,
        phone=_phone(env.nonce, 91),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    accept, save = pipeline.accept, env.store.save
    commands: list[Any] = []

    async def record_save(command: Any) -> Any:
        commands.append(command)
        return await save(command)

    async def change_then_accept(app: Any, request: Any, **kwargs: Any) -> Any:
        proof = request.uncertain_source_proof
        assert proof is not None
        if change == "missing_proof":
            request = replace(request, uncertain_source_proof=None)
        elif change in {"signature", "identity", "created_at", "generation"}:
            if change == "signature":
                proof = replace(proof, signature="0" * 64)
            elif change == "generation":
                proof = replace(proof, generation=proof.generation + 1)
            else:
                source = proof.messages[0]
                source = replace(source, **(
                    {"id": source.id + 100000} if change == "identity" else
                    {"created_at": source.created_at + timedelta(microseconds=1)}
                ))
                proof = replace(proof, messages=(source,))
                # 有效签名的旧身份也必须被真实数据库复核拒绝。
                proof = replace(proof, signature=_crypto().idempotency_fingerprint(
                    proof.canonical(), key_version=proof.key_version,
                ))
            request = replace(request, uncertain_source_proof=proof)
        elif change == "recipient":
            request = replace(request, mobiles=(_phone(env.nonce, 92),))
        elif change == "report_unknown":
            await _r9_report(env, chunk_id, status=0)
        else:
            async with env.engine.begin() as connection:
                sql = ("DELETE FROM sms_message WHERE batch_id=:id" if change == "deleted"
                       else "UPDATE sms_message SET chunk_id=NULL WHERE batch_id=:id")
                await connection.execute(text(sql), {"id": batch_id})
        return await accept(app, request, **kwargs)

    monkeypatch.setattr(env.store, "save", record_save)
    monkeypatch.setattr(pipeline, "accept", change_then_accept)
    with pytest.raises(UncertainResolutionConflict, match="source_message_state_changed"):
        await _apply(env, resolution_id, pipeline, monkeypatch)
    assert len(commands) == 1
    command = commands[0]
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_batch WHERE batch_no=:no"
        ), {"no": command.batch_no}) == 0
        assert await connection.scalar(text(
            "SELECT count(*) FROM outbox_event WHERE aggregate_id=:no "
            "AND event_type='batch.ready'"
        ), {"no": command.batch_no}) == 0
        assert await connection.scalar(text(
            "SELECT state FROM usage_reservation WHERE id=:id"
        ), {"id": command.usage_reservation_id}) in {"release_requested", "released"}
        assert await connection.scalar(text(
            "SELECT state FROM send_inflight_reservation WHERE id=:id"
        ), {"id": command.inflight_reservation_id}) == "released"
        assert await connection.scalar(text(
            "SELECT effect_generation FROM sms_uncertain_resolution WHERE id=:id"
        ), {"id": resolution_id}) == 1


async def _r9_wait_blocked(engine: Any, holder: int) -> None:
    """以 PostgreSQL 实际等待图证明交错，不把时间延迟当作持锁证据。"""
    import asyncio

    async with asyncio.timeout(10):
        while True:
            async with engine.connect() as connection:
                blocked = await connection.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE :holder=ANY(pg_blocking_pids(pid)))"
                ), {"holder": holder})
            if blocked:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["report", "child"])
async def test_r9_report_and_child_actual_lock_order(
    env: Any, monkeypatch: pytest.MonkeyPatch, winner: str,
) -> None:
    import asyncio

    from app.services.report_repository import SqlReportRepository

    batch_id, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="web", dept="运营一部", app_id=None,
        phone=_phone(env.nonce, 93),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    locked, release = asyncio.Event(), asyncio.Event()
    holder: list[int] = []
    original_batch_lock = SqlReportRepository._lock_batch
    original_insert, original_accept = env.store._insert, pipeline.accept
    report_tasks: list[Any] = []

    async def hold_report(connection: Any, target: int) -> None:
        await original_batch_lock(connection, target)
        if target == batch_id and winner == "report":
            holder.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
            locked.set()
            await release.wait()

    async def hold_child(connection: Any, command: Any, number: str) -> Any:
        result = await original_insert(connection, command, number)
        if winner == "child":
            holder.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
            locked.set()
            await release.wait()
        return result

    async def start_report(app: Any, request: Any, **kwargs: Any) -> Any:
        if winner == "report":
            report_tasks.append(asyncio.create_task(_r9_report(env, chunk_id)))
            await locked.wait()
        return await original_accept(app, request, **kwargs)

    monkeypatch.setattr(SqlReportRepository, "_lock_batch", staticmethod(hold_report))
    monkeypatch.setattr(env.store, "_insert", hold_child)
    monkeypatch.setattr(pipeline, "accept", start_report)
    task = asyncio.create_task(_apply(env, resolution_id, pipeline, monkeypatch))
    try:
        await asyncio.wait_for(locked.wait(), 10)
        if winner == "child":
            report_tasks.append(asyncio.create_task(_r9_report(env, chunk_id)))
        await _r9_wait_blocked(env.engine, holder[0])
        release.set()
        if winner == "report":
            with pytest.raises(UncertainResolutionConflict, match="source_message_state_changed"):
                await asyncio.wait_for(task, 15)
        else:
            result = await asyncio.wait_for(task, 15)
            assert result.state == "closed" and result.child_batch_id is not None
        await asyncio.wait_for(asyncio.gather(*report_tasks), 15)
    finally:
        release.set()
        await asyncio.gather(task, *report_tasks, return_exceptions=True)
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_uncertain_child WHERE resolution_id=:id"
        ), {"id": resolution_id}) == (1 if winner == "child" else 0)
        assert await connection.scalar(text(
            "SELECT status FROM sms_chunk WHERE id=:id"
        ), {"id": chunk_id}) == "unknown_terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_r9_rotated_hmac_filtered_mapping_and_mixed_source(
    env: Any, monkeypatch: pytest.MonkeyPatch, changed: bool,
) -> None:
    import base64

    first, second = _phone(env.nonce, 94), _phone(env.nonce, 95)
    app_id = await _insert_api_app(env.engine, env.nonce + "r9")
    batch_id, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="api", dept="运营一部", app_id=app_id, phone=first,
    )
    async with env.engine.begin() as connection:
        for phone in (second, first):
            protected = _crypto().protect_phone(phone)
            await connection.execute(text("""
                INSERT INTO sms_message(batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,
                  key_version,status) VALUES(:batch,:chunk,:enc,:hmac,:mask,:version,'unknown')
            """), {"batch": batch_id, "chunk": chunk_id, "enc": protected.phone_enc,
                    "hmac": protected.phone_hmac, "mask": protected.phone_mask,
                    "version": protected.key_version})
        await connection.execute(text(
            "UPDATE sms_batch SET total=3,unknown_cnt=3,quota_cost=3 WHERE id=:id"
        ), {"id": batch_id})
        await connection.execute(text(
            "UPDATE sms_chunk SET phone_count=3 WHERE id=:id"
        ), {"id": chunk_id})
    new_key = base64.b64encode(b"r" * 32).decode()
    rotated = CryptoService.from_secret_values(
        json.dumps({"active_version": 2, "keys": {"1": _AES, "2": new_key}}),
        json.dumps({"active_version": 2, "keys": {"1": _HMAC, "2": new_key}}),
    )
    env.service.crypto = rotated
    env.store.crypto = rotated
    env.store.blocked = set(rotated.hmac_candidates(first).values())
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    pipeline.crypto = rotated
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    accept = pipeline.accept

    async def late_change(app: Any, request: Any, **kwargs: Any) -> Any:
        if changed:
            # 修改已被黑名单过滤的源消息也应保守拒绝整个旧快照。
            async with env.engine.begin() as connection:
                await connection.execute(text(
                    "UPDATE sms_message SET report_status=1,status='delivered' "
                    "WHERE batch_id=:id AND phone_hmac=:hmac"
                ), {"id": batch_id, "hmac": _crypto().phone_hmac(first)})
        return await accept(app, request, **kwargs)

    monkeypatch.setattr(pipeline, "accept", late_change)
    if changed:
        with pytest.raises(UncertainResolutionConflict, match="source_message_state_changed"):
            await _apply(env, resolution_id, pipeline, monkeypatch)
    else:
        result = await _apply(env, resolution_id, pipeline, monkeypatch)
        async with env.engine.connect() as connection:
            child = (await connection.execute(text(
                "SELECT total,removed_duplicate,removed_blacklist FROM sms_batch WHERE id=:id"
            ), {"id": result.child_batch_id})).mappings().one()
            assert dict(child) == {"total": 1, "removed_duplicate": 1, "removed_blacklist": 1}
            message = (await connection.execute(text(
                "SELECT phone_hmac,key_version FROM sms_message WHERE batch_id=:id"
            ), {"id": result.child_batch_id})).mappings().one()
            assert message["key_version"] == 2
            assert message["phone_hmac"].strip() == rotated.phone_hmac(second)


@pytest.mark.asyncio
async def test_r9_lost_commit_reply_recovers_child_after_late_report_without_new_proof(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, chunk_id = await _insert_unknown(
        env.engine, _crypto(), channel="web", dept="运营一部", app_id=None,
        phone=_phone(env.nonce, 96),
    )
    resolution_id = await _approve(env.service, chunk_id, env.proposer, env.confirmer)
    pipeline = _pipeline(env.store, env.ledger, env.redis)
    accept = pipeline.accept
    commands: list[Any] = []
    save = env.store.save

    async def record_save(command: Any) -> Any:
        commands.append(command)
        return await save(command)

    async def lose_reply(*args: Any, **kwargs: Any) -> Any:
        await accept(*args, **kwargs)
        raise ConnectionError("synthetic commit reply loss")

    monkeypatch.setattr(env.store, "save", record_save)
    monkeypatch.setattr(pipeline, "accept", lose_reply)
    with pytest.raises(ConnectionError):
        await _apply(env, resolution_id, pipeline, monkeypatch)
    await _r9_report(env, chunk_id)
    # 同一可信 child 的保存恢复也不要求重建旧来源证明。
    from dataclasses import replace

    command = commands[0]
    with audit_principal_scope(command.principal), correlation_scope(uuid4()):
        recovered = await save(replace(command, uncertain_source_proof=None))
    assert recovered.idempotent and recovered.batch_no == command.batch_no

    async def no_new_prepare(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("trusted child recovery must precede source preparation")

    monkeypatch.setattr(env.service, "_build_resend", no_new_prepare)
    closed = await _apply(env, resolution_id, pipeline, monkeypatch)
    repeated = await _apply(env, resolution_id, pipeline, monkeypatch)
    assert repeated.child_batch_id == closed.child_batch_id
    assert len(commands) == 1
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT state FROM usage_reservation WHERE id=:id"
        ), {"id": command.usage_reservation_id}) == "committed"
        assert await connection.scalar(text(
            "SELECT state FROM send_inflight_reservation WHERE id=:id"
        ), {"id": command.inflight_reservation_id}) == "batch_bound"


async def _r10_case(env: Any, sibling: str = "split_capacity_blocked") -> Any:
    """真实 Pipeline/Usage Ledger 受理后构造两个合成生命周期分片。"""
    from app.core.auth.accounts import ApplicationPrincipal
    from app.services.pipeline import SendRequest
    from app.services.send_inflight import materialize_in_flight_reservation

    app_id = await _insert_api_app(env.engine, env.nonce + "r10", categories="verify")
    app = ApiAppContext(app_id, "r10", "研发部", frozenset({"verify"}), daily_quota=1000)
    actor = ApplicationPrincipal(app_id, "r10", "研发部")
    with audit_principal_scope(actor), correlation_scope(uuid4()):
        response = await _pipeline(env.store, env.ledger, env.redis).accept(app, SendRequest(
            category="verify", mobiles=tuple(_phone(env.nonce, x) for x in (110, 111, 112)),
            content="合成验证通知", channel="api", actor=actor, biz_id=uuid4().hex,
        ))
    async with env.engine.begin() as connection:
        batch = (await connection.execute(text(
            "SELECT id,usage_reservation_id FROM sms_batch WHERE batch_no=:no"
        ), {"no": response.batch_no})).mappings().one()
        chunks = []
        for number, status, count in ((1, "unknown_terminal", 1), (2, sibling, 2)):
            chunks.append(int(await connection.scalar(text("""
                INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
                VALUES(:batch,:number,:custom,:count,:status) RETURNING id
            """), {"batch": batch["id"], "number": number, "custom": uuid4().hex,
                    "count": count, "status": status})))
        messages = (await connection.execute(text(
            "SELECT id,created_at FROM sms_message WHERE batch_id=:id ORDER BY id"
        ), {"id": batch["id"]})).mappings().all()
        sibling_message = "unknown" if sibling in {"unknown_terminal", "uncertain"} else (
            "failed" if sibling == "failed" else "pending"
        )
        for index, row in enumerate(messages):
            await connection.execute(text(
                "UPDATE sms_message SET chunk_id=:chunk,status=:status "
                "WHERE id=:id AND created_at=:at"
            ), {"chunk": chunks[0 if index == 0 else 1],
                "status": "unknown" if index == 0 else sibling_message,
                "id": row["id"], "at": row["created_at"]})
        for chunk_id, count in zip(chunks, (1, 2), strict=True):
            await connection.execute(text("""
                INSERT INTO usage_chunk_allocation(chunk_id,batch_id,reservation_id,
                    recipient_count,segment_count,request_count,app_id)
                VALUES(:chunk,:batch,:reservation,:count,:count,0,:app)
            """), {"chunk": chunk_id, "batch": batch["id"],
                    "reservation": batch["usage_reservation_id"], "count": count, "app": app_id})
        await materialize_in_flight_reservation(
            connection, batch_id=batch["id"], actual_chunks=2, limit=200,
        )
        await connection.execute(text(
            "UPDATE sms_batch SET status='sending',unknown_cnt=1 WHERE id=:id"
        ), {"id": batch["id"]})
    proposed = await env.service.propose(chunks[0], "confirm_not_accepted", env.proposer)
    resolution = await env.service.confirm(proposed.id, env.confirmer)
    return SimpleNamespace(batch_id=batch["id"], reservation=batch["usage_reservation_id"],
                           chunks=chunks, resolution=resolution.id, app_id=app_id)


async def _r10_snapshot(env: Any, case: Any) -> Any:
    async with env.engine.connect() as connection:
        state = await connection.scalar(text(
            "SELECT state FROM usage_reservation WHERE id=:id"
        ), {"id": case.reservation})
        projections = (await connection.execute(text("""
            SELECT dimension_key,value,version FROM usage_projection WHERE dimension_key IN (
              SELECT projection_key FROM usage_quota_entry WHERE reservation_id=:id
              UNION SELECT projection_key FROM usage_frequency_entry WHERE reservation_id=:id
            ) ORDER BY dimension_key
        """), {"id": case.reservation})).all()
        outbox = await connection.scalar(text(
            "SELECT count(*) FROM outbox_event WHERE event_type='usage.release' "
            "AND aggregate_id=:id"
        ), {"id": str(case.reservation)})
        inflight = (await connection.execute(text(
            "SELECT state,reserved_chunks FROM send_inflight_reservation WHERE batch_id=:id"
        ), {"id": case.batch_id})).one()
    return state, projections, outbox, inflight


@pytest.mark.asyncio
async def test_r10_blocked_split_preserves_whole_usage(env: Any) -> None:
    case = await _r10_case(env)
    before = await _r10_snapshot(env, case)
    result = await env.service.apply_effect(case.resolution)
    assert result.state == "closed"
    assert await _r10_snapshot(env, case) == before
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM usage_chunk_release WHERE resolution_id=:id"
        ), {"id": case.resolution}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    "pending", "retrying", "submitting", "failover_pending", "split_capacity_blocked",
    "uncertain", "unknown_terminal", "submitted", "failed",
])
async def test_r10_chunk_lifecycle_matrix(env: Any, status: str) -> None:
    case = await _r10_case(env, status)
    before = await _r10_snapshot(env, case)
    await env.service.apply_effect(case.resolution)
    after = await _r10_snapshot(env, case)
    if status == "failed":
        assert after[0] == "release_requested" and after[2] == 1
        async with env.engine.connect() as connection:
            amounts = (await connection.execute(text("""
                SELECT projection_key,sum(amount) FROM (
                  SELECT projection_key,amount FROM usage_quota_entry WHERE reservation_id=:id
                  UNION ALL SELECT projection_key,CASE WHEN counted THEN 1 ELSE 0 END
                  FROM usage_frequency_entry WHERE reservation_id=:id
                ) e GROUP BY projection_key
            """), {"id": case.reservation})).all()
        previous = {row[0]: row[1] for row in before[1]}
        actual = {row[0]: row[1] for row in after[1]}
        assert {key: previous[key]-actual[key] for key, _ in amounts} == dict(amounts)
        assert after[3] == before[3]
        await env.service.apply_effect(case.resolution)
        assert await _r10_snapshot(env, case) == after
        await env.ledger.apply_release(case.reservation)
        await env.ledger.apply_release(case.reservation)
        assert (await _r10_snapshot(env, case))[0] == "released"
    else:
        assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "pending_message", "orphan_message", "report_fields", "allocation", "generation", "legacy",
])
async def test_r10_incoherent_facts_fail_closed(env: Any, damage: str) -> None:
    case = await _r10_case(env, "failed")
    async with env.engine.begin() as connection:
        if damage in {"pending_message", "orphan_message", "report_fields"}:
            assignment = {"pending_message": "status='pending'", "orphan_message": "chunk_id=NULL",
                          "report_fields": "report_status=1"}[damage]
            await connection.execute(text(
                f"UPDATE sms_message SET {assignment} WHERE chunk_id=:id"
            ), {"id": case.chunks[1]})
        elif damage == "allocation":
            await connection.execute(text(
                "UPDATE usage_chunk_allocation SET app_id=NULL WHERE chunk_id=:id"
            ), {"id": case.chunks[1]})
        else:
            # 不完整历史事实及不同代次的事实均不得被本次 effect 自动认领。
            await connection.execute(text("""
                INSERT INTO usage_chunk_release(resolution_id,chunk_id,reservation_id,
                  effect_generation,recipient_count,segment_count,request_count,release_event_id)
                SELECT :resolution,chunk_id,reservation_id,:generation,recipient_count,
                  segment_count,request_count,:event FROM usage_chunk_allocation WHERE chunk_id=:id
            """), {"resolution": case.resolution, "id": case.chunks[0],
                    "generation": None if damage == "legacy" else 2,
                    "event": f"resolution:{case.resolution}:not-accepted"})
    before = await _r10_snapshot(env, case)
    if damage in {"legacy", "generation"}:
        with pytest.raises(UncertainResolutionConflict):
            await env.service.apply_effect(case.resolution)
    else:
        await env.service.apply_effect(case.resolution)
    assert await _r10_snapshot(env, case) == before


@pytest.mark.asyncio
async def test_r10_split_resume_then_controlled_reevaluation(env: Any) -> None:
    from app.tasks.send_repository import retry_capacity_blocked_splits

    case = await _r10_case(env)
    before = await _r10_snapshot(env, case)
    await env.service.apply_effect(case.resolution)
    async with env.engine.begin() as connection:
        assert await retry_capacity_blocked_splits(connection, limit=10000) >= 1
        children = (await connection.execute(text(
            "SELECT id FROM sms_chunk WHERE parent_chunk_id=:id"
        ), {"id": case.chunks[1]})).scalars().all()
        assert len(children) == 2
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_message WHERE chunk_id=ANY(:ids) AND status='pending'"
        ), {"ids": list(children)}) == 2
        assert await connection.scalar(text(
            "SELECT count(*) FROM usage_chunk_allocation WHERE chunk_id=ANY(:ids)"
        ), {"ids": list(children)}) == 2
        assert await connection.scalar(text(
            "SELECT count(*) FROM outbox_event WHERE event_type='chunk.ready' "
            "AND aggregate_id=ANY(:ids)"
        ), {"ids": [str(child) for child in children]}) == 2
    await env.service.apply_effect(case.resolution)
    after = await _r10_snapshot(env, case)
    assert after[:3] == before[:3]
    assert after[3][1] == before[3][1] + 1
    # 合成确定性未受理终止；保留 A 的原确认后重放同一受控 effect 结算。
    async with env.engine.begin() as connection:
        await connection.execute(text(
            "UPDATE sms_chunk SET status='failed' WHERE id=ANY(:ids)"
        ), {"ids": list(children)})
        await connection.execute(text(
            "UPDATE sms_message SET status='failed' WHERE chunk_id=ANY(:ids)"
        ), {"ids": list(children)})
    await env.service.apply_effect(case.resolution)
    assert (await _r10_snapshot(env, case))[0] == "release_requested"


@pytest.mark.asyncio
async def test_r10_concurrent_confirmations_and_duplicate_effect(env: Any) -> None:
    import asyncio

    case = await _r10_case(env, "unknown_terminal")
    proposed = await env.service.propose(case.chunks[1], "confirm_not_accepted", env.proposer)
    other = await env.service.confirm(proposed.id, env.confirmer)
    await asyncio.wait_for(asyncio.gather(
        env.service.apply_effect(case.resolution), env.service.apply_effect(other.id),
    ), 10)
    once = await _r10_snapshot(env, case)
    assert once[0] == "release_requested" and once[2] == 1
    await asyncio.wait_for(asyncio.gather(
        env.service.apply_effect(case.resolution), env.service.apply_effect(case.resolution),
        env.service.apply_effect(other.id),
    ), 10)
    assert await _r10_snapshot(env, case) == once
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM usage_chunk_release WHERE chunk_id=ANY(:ids)"
        ), {"ids": case.chunks}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["fact", "eligibility", "release", "outbox_before", "outbox_after"],
)
async def test_r10_fault_rolls_back_fact_projection_and_outbox(
    env: Any, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    import app.services.uncertain_resolution as module
    import app.services.usage_ledger as ledger_module

    case = await _r10_case(env, "failed")
    before = await _r10_snapshot(env, case)
    eligibility = module._all_chunks_not_accepted
    release = module.request_usage_release_for_batch
    outbox = ledger_module.enqueue_outbox

    async def fail_eligibility(*args: Any, **kwargs: Any) -> Any:
        if phase == "eligibility":
            assert await eligibility(*args, **kwargs)
        raise RuntimeError("synthetic release fault")

    async def fail_release(*args: Any, **kwargs: Any) -> Any:
        await release(*args, **kwargs)
        raise RuntimeError("synthetic release fault")

    async def fail_outbox(*args: Any, **kwargs: Any) -> Any:
        if phase == "outbox_after":
            await outbox(*args, **kwargs)
        raise RuntimeError("synthetic release fault")

    with monkeypatch.context() as patch:
        if phase in {"fact", "eligibility"}:
            patch.setattr(module, "_all_chunks_not_accepted", fail_eligibility)
        elif phase == "release":
            patch.setattr(module, "request_usage_release_for_batch", fail_release)
        else:
            patch.setattr(ledger_module, "enqueue_outbox", fail_outbox)
        with pytest.raises(RuntimeError, match="synthetic release fault"):
            await env.service.apply_effect(case.resolution)
    assert await _r10_snapshot(env, case) == before
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM usage_chunk_release WHERE resolution_id=:id"
        ), {"id": case.resolution}) == 0
    await env.service.apply_effect(case.resolution)
    once = await _r10_snapshot(env, case)
    await env.service.apply_effect(case.resolution)
    assert once == await _r10_snapshot(env, case)
    assert once[0] == "release_requested" and once[2] == 1


@pytest.mark.asyncio
async def test_r10_commit_reply_loss_and_expired_window_preserve_new_window(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    import app.services.usage_ledger as ledger_module

    case = await _r10_case(env, "failed")
    future = datetime.now(UTC) + timedelta(days=2)
    async with env.engine.begin() as connection:
        key = await connection.scalar(text(
            "SELECT projection_key FROM usage_frequency_entry WHERE reservation_id=:id LIMIT 1"
        ), {"id": case.reservation})
        assert key is not None
        await connection.execute(text(
            "UPDATE usage_projection SET window_key=:window,value=7,expires_at=:expires "
            "WHERE dimension_key=:key"
        ), {"key": key, "window": future.strftime("%Y%m%d%H%M"),
            "expires": future + timedelta(days=1)})

    async def future_now(_connection: Any) -> Any:
        return future

    monkeypatch.setattr(ledger_module, "_database_now", future_now)
    run = env.service._run_not_accepted

    async def lose_reply(current: Any) -> None:
        await run(current)
        raise ConnectionError("synthetic commit reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(env.service, "_run_not_accepted", lose_reply)
        with pytest.raises(ConnectionError):
            await env.service.apply_effect(case.resolution)
    once = await _r10_snapshot(env, case)
    assert once[0] == "release_requested" and once[2] == 1
    assert await env.service.apply_effect(case.resolution)
    assert await _r10_snapshot(env, case) == once
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT value FROM usage_projection WHERE dimension_key=:key"
        ), {"key": key}) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["report", "split"])
@pytest.mark.parametrize("winner", ["release", "writer"])
async def test_r10_actual_writer_and_release_lock_order(
    env: Any, monkeypatch: pytest.MonkeyPatch, writer: str, winner: str,
) -> None:
    import asyncio

    import app.services.uncertain_resolution as resolution_module
    from app.services.report_repository import SqlReportRepository
    from app.tasks.send import ChunkPayload
    from app.tasks.send_repository import complete_vendor_split

    case = await _r10_case(env, "failed" if writer == "report" else "split_capacity_blocked")
    locked, proceed = asyncio.Event(), asyncio.Event()
    holder: list[int] = []
    eligibility = resolution_module._all_chunks_not_accepted
    batch_lock = SqlReportRepository._lock_batch

    async def hold(connection: Any) -> None:
        holder.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
        locked.set()
        await proceed.wait()

    async def held_eligibility(connection: Any, batch_id: int) -> bool:
        result = await eligibility(connection, batch_id)
        if winner == "release" and batch_id == case.batch_id:
            await hold(connection)
        return result

    async def held_report(connection: Any, batch_id: int) -> None:
        await batch_lock(connection, batch_id)
        if winner == "writer" and batch_id == case.batch_id:
            await hold(connection)

    async def run_writer() -> None:
        if writer == "report":
            await _r9_report(env, case.chunks[1])
        else:
            async with env.engine.begin() as connection:
                custom_id = await connection.scalar(text(
                    "SELECT trim(custom_id) FROM sms_chunk WHERE id=:id"
                ), {"id": case.chunks[1]})
                children = await complete_vendor_split(connection, ChunkPayload(
                    chunk_id=case.chunks[1], batch_id=case.batch_id, custom_id=custom_id,
                    phones=("1" * 11, "1" * 11), content="", template_id="", sign_name="",
                ))
                assert len(children) == 2
                if winner == "writer":
                    await hold(connection)

    monkeypatch.setattr(resolution_module, "_all_chunks_not_accepted", held_eligibility)
    monkeypatch.setattr(SqlReportRepository, "_lock_batch", staticmethod(held_report))
    async def release_call() -> Any:
        return await env.service.apply_effect(case.resolution)
    first = asyncio.create_task(release_call() if winner == "release" else run_writer())
    tasks = [first]
    try:
        await asyncio.wait_for(locked.wait(), 10)
        tasks.append(asyncio.create_task(run_writer() if winner == "release" else release_call()))
        await _r9_wait_blocked(env.engine, holder[0])
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        proceed.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    actual = await _r10_snapshot(env, case)
    if writer == "report" and winner == "release":
        assert actual[0] == "release_requested" and actual[2] == 1
    else:
        assert actual[0] == "committed" and actual[2] == 0


@pytest.mark.asyncio
async def test_r10_generation_changed_after_effect_claim_cannot_write_fact(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _r10_case(env, "failed")
    original = env.service._run_not_accepted

    async def change_generation(current: Any) -> None:
        async with env.engine.begin() as connection:
            await connection.execute(text(
                "UPDATE sms_uncertain_resolution SET effect_generation=2 WHERE id=:id"
            ), {"id": case.resolution})
        await original(current)

    monkeypatch.setattr(env.service, "_run_not_accepted", change_generation)
    before = await _r10_snapshot(env, case)
    with pytest.raises(UncertainResolutionConflict):
        await env.service.apply_effect(case.resolution)
    assert await _r10_snapshot(env, case) == before
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM usage_chunk_release WHERE resolution_id=:id"
        ), {"id": case.resolution}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "split_capacity_blocked"])
async def test_r10_send_runtime_release_permissions(
    env: Any, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    case = await _r10_case(env, status)
    runtime = await _send_runtime(env, monkeypatch)
    try:
        result = await _apply_as_send(runtime, case.resolution, monkeypatch)
        assert result.state == "closed"
        expected = "release_requested" if status == "failed" else "committed"
        assert (await _r10_snapshot(env, case))[0] == expected
    finally:
        await runtime.engine.dispose()


@pytest.mark.asyncio
async def test_r10_completed_outbox_then_real_failure_finalization_releases_usage(
    env: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.uncertain_resolution as resolution_module
    import app.tasks.outbox as tasks
    from app.services.outbox_repository import SqlOutboxRepository
    from app.tasks.send_repository import SqlChunkStore, retry_capacity_blocked_splits

    case = await _r10_case(env)

    class Repository(SqlOutboxRepository):
        def _engine(self) -> Any:
            return _NoDisposeEngine(env.engine)

    repository = Repository(settings=env.settings)
    monkeypatch.setattr(tasks, "SqlOutboxRepository", lambda _settings: repository)
    monkeypatch.setattr(
        resolution_module, "UncertainResolutionService", lambda _crypto: env.service,
    )
    monkeypatch.setattr(
        CryptoService, "from_settings", classmethod(lambda cls, settings: _crypto()),
    )
    async with env.engine.begin() as connection:
        event_id = await connection.scalar(text(
            "SELECT id FROM outbox_event WHERE dedup_key=:key"
        ), {"key": f"uncertain.effect:{case.resolution}:1"})
        # 队列发布完成的合成边界；实际认领、effect、完成与重复拒绝全部使用仓库代码。
        await connection.execute(text(
            "UPDATE outbox_event SET state='published',attempts=1,lease_id=:lease,"
            "lease_expires_at=now()+interval '60 seconds' WHERE id=:id"
        ), {"id": event_id, "lease": uuid4()})
    assert await tasks._apply_uncertain_effect(case.resolution, str(event_id)) == 1
    async with env.engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT state FROM outbox_event WHERE id=:id"
        ), {"id": event_id}) == "completed"
    assert await tasks._apply_uncertain_effect(case.resolution, str(event_id)) == 0
    assert (await _r10_snapshot(env, case))[0] == "committed"
    async with env.engine.begin() as connection:
        await retry_capacity_blocked_splits(connection, limit=10000)
        children = (await connection.execute(text(
            "SELECT id FROM sms_chunk WHERE parent_chunk_id=:id ORDER BY id"
        ), {"id": case.chunks[1]})).scalars().all()
    store = SqlChunkStore(_crypto(), settings=env.settings, redis=env.redis)
    monkeypatch.setattr(store, "_engine", lambda: _NoDisposeEngine(env.engine))
    assert len(children) == 2
    for child in children:
        assert await store.mark_submitting(child, 0)
        await store.mark_failed(child, 1002, "synthetic deterministic reject")
    settled = await _r10_snapshot(env, case)
    assert settled[0] == "release_requested" and settled[2] == 1
    assert await tasks._apply_uncertain_effect(case.resolution, str(event_id)) == 0
    assert await _r10_snapshot(env, case) == settled
