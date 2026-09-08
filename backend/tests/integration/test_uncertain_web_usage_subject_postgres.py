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
        super().__init__(settings=settings)
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
