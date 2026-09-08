from __future__ import annotations

import asyncio
import base64
import os
import signal
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import ApplicationPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.app_ratelimit import ControlPlaneUnavailable
from app.services.crypto import CryptoService
from app.services.idempotency import IdempotencyCoordinator, IdempotencyScope, parse_claim_payload
from app.services.pipeline import BatchCommand, StoredBatch
from app.services.pipeline_repository import SqlPipelineStore
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)

_AES = base64.b64encode(b"v" * 32).decode()
_HMAC = base64.b64encode(b"v" * 32).decode()
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(_AES, _HMAC)


class EngineBoundStore(SqlPipelineStore):
    """测试用本地 engine，避免跨用例复用 database_engine 事件循环。"""

    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(settings=settings)
        self._bound_engine = engine
        sync_engine = engine.sync_engine
        if not getattr(sync_engine, "_sms_claim_audit_begin", False):
            event.listen(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_claim_audit_begin = True

    def _engine(self) -> Any:
        return self._bound_engine


def _store(engine: Any, database_url: Any) -> EngineBoundStore:
    return EngineBoundStore(
        engine,
        settings=cast(Any, SimpleNamespace(database_url=database_url)),
    )


def child_hold_claim() -> None:
    """独立进程占用 Claim，供父进程 SIGKILL 后验证接管。"""

    async def _claim() -> str:
        redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
        database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
        engine = create_async_engine(database_url, hide_parameters=True)
        store = _store(engine, database_url)
        coordinator = IdempotencyCoordinator(
            redis,
            store,
            claim_ttl_s=int(os.environ["SMS_CLAIM_TTL"]),
        )
        scope = IdempotencyScope("app", os.environ["SMS_CLAIM_SCOPE"])
        try:
            token = await coordinator.claim(
                scope,
                os.environ["SMS_CLAIM_BIZ"],
                fingerprint=os.environ["SMS_CLAIM_FP"],
            )
            if token is None:
                raise AssertionError("child must own the claim")
            return token
        finally:
            await redis.aclose()
            await engine.dispose()

    token = asyncio.run(_claim())
    Path(os.environ["SMS_CLAIM_TOKEN"]).write_text(token, encoding="utf-8")
    Path(os.environ["SMS_CLAIM_READY"]).write_text("ready", encoding="utf-8")
    time.sleep(60)


async def _prepare_db(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _insert_app(engine: Any, nonce: str) -> int:
    async with engine.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,created_by
                        ) VALUES(
                          :name,'平台部',:api_key_hash,:api_key_prefix,'test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"claim-{nonce}",
                        "api_key_hash": "b" * 64,
                        "api_key_prefix": nonce[:8],
                    },
                )
            ).scalar_one()
        )


def _command(
    *,
    app_id: int,
    biz_id: str,
    token: str,
    generation: int,
    fingerprint: str,
) -> BatchCommand:
    protected = _crypto().protect_phone("13800138000")
    return BatchCommand(
        batch_no=uuid4().hex,
        app_id=app_id,
        dept="平台部",
        category="notice",
        channel="api",
        display_content_enc=b"display",
        send_content_enc=b"send",
        sign_name=None,
        template_id=None,
        biz_id=biz_id,
        segments=1,
        quota_cost=1,
        status="queued",
        deferred_reason=None,
        scheduled_at=None,
        removed_duplicate=0,
        removed_blacklist=0,
        removed_freq=0,
        principal=ApplicationPrincipal(app_id, "claim-app", "平台部"),
        approval_expire_hours=24,
        approval_threshold=None,
        is_test=False,
        consent_confirmed=False,
        remark=None,
        resend_of=None,
        usage_reservation_id=None,
        import_reservation_id=None,
        messages=(protected,),
        scope_kind="app",
        scope_id=str(app_id),
        request_hash=fingerprint,
        request_hash_key_version=1,
        idempotency_claim_token=token,
        idempotency_claim_generation=generation,
    )


async def _complete(
    store: SqlPipelineStore,
    *,
    app_id: int,
    biz_id: str,
    token: str,
    generation: int,
    fingerprint: str,
) -> StoredBatch:
    principal = ApplicationPrincipal(app_id, "claim-app", "平台部")
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        return await store.save(
            _command(
                app_id=app_id,
                biz_id=biz_id,
                token=token,
                generation=generation,
                fingerprint=fingerprint,
            )
        )


async def _claim_row(engine: Any, scope: IdempotencyScope, biz_id: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT state, generation, batch_id, token,
                           expires_at > now() AS lease_valid
                    FROM idempotency_claim
                    WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                      AND biz_id=:biz_id
                    """
                ),
                {
                    "scope_kind": scope.kind,
                    "scope_id": scope.id,
                    "biz_id": biz_id,
                },
            )
        ).mappings().one()
    return dict(row)


@pytest_asyncio.fixture
async def claim_env() -> Any:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    await _prepare_db(engine)
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce)
    store = _store(engine, database_url)
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    try:
        yield engine, store, redis, app_id
    finally:
        if getattr(engine.sync_engine, "_sms_claim_audit_begin", False):
            event.remove(engine.sync_engine, "begin", _set_audit_transaction_context)
            engine.sync_engine._sms_claim_audit_begin = False
        await redis.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_completes_within_five_seconds(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"c5-{uuid4().hex[:12]}"
    fingerprint = "a" * 64
    coordinator = IdempotencyCoordinator(redis, store)
    token = await coordinator.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    viewed = await coordinator.inspect(scope, biz_id)
    assert viewed is not None
    stored = await _complete(
        store,
        app_id=app_id,
        biz_id=biz_id,
        token=token,
        generation=viewed.generation,
        fingerprint=fingerprint,
    )
    await coordinator.release(scope, biz_id, token)
    row = await _claim_row(engine, scope, biz_id)
    assert stored.idempotent is False
    assert stored.outbox_persisted is True
    assert row["state"] == "completed"
    assert row["batch_id"] is not None
    assert await store.find_existing(scope, biz_id) == stored.batch_no


@pytest.mark.asyncio
async def test_legal_owner_completes_after_35_60_90_120_seconds(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, _redis, app_id = claim_env
    fingerprint = "b" * 64

    async def _hold(seconds: int) -> str:
        scope = IdempotencyScope("app", str(app_id))
        biz_id = f"c{seconds}-{uuid4().hex[:10]}"
        client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
        coordinator = IdempotencyCoordinator(client, store, claim_ttl_s=30)
        token = await coordinator.claim(scope, biz_id, fingerprint=fingerprint)
        assert token is not None
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            coordinator.heartbeat(scope, biz_id, token, lost)
        )
        try:
            await asyncio.sleep(seconds)
            assert not lost.is_set()
            viewed = await coordinator.inspect(scope, biz_id)
            assert viewed is not None
            stored = await _complete(
                store,
                app_id=app_id,
                biz_id=biz_id,
                token=token,
                generation=viewed.generation,
                fingerprint=fingerprint,
            )
            row = await _claim_row(engine, scope, biz_id)
            assert row["state"] == "completed"
            return stored.batch_no
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await coordinator.release(scope, biz_id, token)
            await client.aclose()

    batches = await asyncio.gather(_hold(35), _hold(60), _hold(90), _hold(120))
    assert len(set(batches)) == 4


@pytest.mark.asyncio
async def test_db_renew_and_redis_partial_failures(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    _engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"part-{uuid4().hex[:12]}"
    coordinator = IdempotencyCoordinator(redis, store)
    token = await coordinator.claim(scope, biz_id, fingerprint="c" * 64)
    assert token is not None
    viewed = await coordinator.inspect(scope, biz_id)
    assert viewed is not None

    class RedisDown:
        async def eval(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        async def get(self, key: str) -> str | None:
            return await redis.get(key)

        async def set(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        async def delete(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("redis down")

    failing = IdempotencyCoordinator(cast(Any, RedisDown()), store)
    failing._payloads[failing.claim_key(scope, biz_id)] = (
        f"{token}:{'c' * 64}:{viewed.generation}"
    )
    with pytest.raises(ControlPlaneUnavailable):
        await failing.renew(scope, biz_id, token)
    row = await store.load_idempotency_claim(scope, biz_id)
    assert row is not None
    assert row["state"] == "active"
    assert bool(row["lease_valid"]) is True

    stale = IdempotencyCoordinator(redis, store)
    stale._payloads[stale.claim_key(scope, biz_id)] = f"{'0' * 32}:{'c' * 64}:1"
    assert await stale.renew(scope, biz_id, "0" * 32) is False
    assert await coordinator.renew(scope, biz_id, token) is True


@pytest.mark.asyncio
async def test_db_claim_redis_initial_write_failure_keeps_owner(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    _engine, store, _unused_redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"ghost-{uuid4().hex[:12]}"

    class RedisDown:
        async def eval(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        async def get(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def set(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        async def delete(self, *_args: Any, **_kwargs: Any) -> Any:
            return None

    coordinator = IdempotencyCoordinator(cast(Any, RedisDown()), store)
    with pytest.raises(ControlPlaneUnavailable):
        await coordinator.claim(scope, biz_id, fingerprint="d" * 64)
    row = await store.load_idempotency_claim(scope, biz_id)
    assert row is not None
    assert row["state"] == "active"
    assert int(row["generation"]) == 1


@pytest.mark.asyncio
async def test_redis_flush_rebuilds_active_and_completed(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    live_biz = f"flush-a-{uuid4().hex[:10]}"
    done_biz = f"flush-c-{uuid4().hex[:10]}"
    fingerprint = "e" * 64
    live = IdempotencyCoordinator(redis, store)
    done = IdempotencyCoordinator(redis, store)
    live_token = await live.claim(scope, live_biz, fingerprint=fingerprint)
    done_token = await done.claim(scope, done_biz, fingerprint=fingerprint)
    assert live_token is not None and done_token is not None
    done_view = await done.inspect(scope, done_biz)
    assert done_view is not None
    stored = await _complete(
        store,
        app_id=app_id,
        biz_id=done_biz,
        token=done_token,
        generation=done_view.generation,
        fingerprint=fingerprint,
    )
    await redis.delete(live.claim_key(scope, live_biz), done.claim_key(scope, done_biz))
    rebuilt = await live.inspect(scope, live_biz)
    assert rebuilt is not None
    assert rebuilt.token == live_token
    assert await redis.get(live.claim_key(scope, live_biz)) is not None
    replay = await done.wait(scope, done_biz)
    assert replay == stored.batch_no
    completed = await _claim_row(engine, scope, done_biz)
    assert completed["state"] == "completed"


@pytest.mark.asyncio
async def test_old_master_low_generation_cannot_overwrite_owner(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"oldm-{uuid4().hex[:12]}"
    fingerprint = "f" * 64
    first = IdempotencyCoordinator(redis, store, claim_ttl_s=2)
    token = await first.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE idempotency_claim
                SET expires_at=now()-interval '1 second'
                WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                  AND biz_id=:biz_id
                """
            ),
            {"scope_kind": scope.kind, "scope_id": scope.id, "biz_id": biz_id},
        )
    second = IdempotencyCoordinator(redis, store)
    token2 = await second.claim(scope, biz_id, fingerprint=fingerprint)
    assert token2 is not None
    viewed = await second.inspect(scope, biz_id)
    assert viewed is not None
    assert viewed.generation == 2
    await redis.set(second.claim_key(scope, biz_id), f"{token}:{fingerprint}:1")
    assert await second.renew(scope, biz_id, token2) is True
    raw = await redis.get(second.claim_key(scope, biz_id))
    assert raw is not None
    assert parse_claim_payload(raw).generation == 2
    assert parse_claim_payload(raw).token == token2


@pytest.mark.asyncio
async def test_expired_lease_takeover_and_stale_owner_cannot_finish(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"stale-{uuid4().hex[:12]}"
    fingerprint = "1" * 64
    owner = IdempotencyCoordinator(redis, store)
    token = await owner.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    old_view = await owner.inspect(scope, biz_id)
    assert old_view is not None
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE idempotency_claim
                SET expires_at=now()-interval '1 second'
                WHERE scope_kind=:scope_kind AND scope_id=:scope_id
                  AND biz_id=:biz_id
                """
            ),
            {"scope_kind": scope.kind, "scope_id": scope.id, "biz_id": biz_id},
        )
    successor = IdempotencyCoordinator(redis, store)
    token2 = await successor.claim(scope, biz_id, fingerprint=fingerprint)
    assert token2 is not None
    new_view = await successor.inspect(scope, biz_id)
    assert new_view is not None
    assert new_view.generation == old_view.generation + 1
    assert await owner.renew(scope, biz_id, token) is False
    await owner.release(scope, biz_id, token)
    successor_row = await _claim_row(engine, scope, biz_id)
    assert successor_row["state"] == "active"
    assert int(successor_row["generation"]) == new_view.generation
    with pytest.raises(RuntimeError, match="claim lost"):
        await store.save(
            _command(
                app_id=app_id,
                biz_id=biz_id,
                token=token,
                generation=old_view.generation,
                fingerprint=fingerprint,
            )
        )
    stored = await _complete(
        store,
        app_id=app_id,
        biz_id=biz_id,
        token=token2,
        generation=new_view.generation,
        fingerprint=fingerprint,
    )
    assert stored.idempotent is False


@pytest.mark.asyncio
async def test_completed_claim_survives_commit_ack_loss_and_cannot_be_taken(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"ack-{uuid4().hex[:12]}"
    fingerprint = "2" * 64
    coordinator = IdempotencyCoordinator(redis, store)
    token = await coordinator.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    viewed = await coordinator.inspect(scope, biz_id)
    assert viewed is not None
    stored = await _complete(
        store,
        app_id=app_id,
        biz_id=biz_id,
        token=token,
        generation=viewed.generation,
        fingerprint=fingerprint,
    )
    await coordinator.release(scope, biz_id, token)
    await redis.delete(coordinator.claim_key(scope, biz_id), coordinator.key(scope, biz_id))
    recovered = await coordinator.wait(scope, biz_id)
    assert recovered == stored.batch_no
    row = await _claim_row(engine, scope, biz_id)
    assert row["state"] == "completed"
    assert await coordinator.claim(scope, biz_id, fingerprint=fingerprint) is None
    async with engine.connect() as connection:
        outbox = (
            await connection.execute(
                text(
                    """
                    SELECT 1 FROM outbox_event
                    WHERE aggregate_id=:batch_no AND event_type='batch.ready'
                    """
                ),
                {"batch_no": stored.batch_no},
            )
        ).scalar_one_or_none()
    assert outbox is not None


@pytest.mark.asyncio
async def test_two_instances_same_biz_id_and_fingerprint_conflict(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    _engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"race-{uuid4().hex[:12]}"
    first = IdempotencyCoordinator(redis, store)
    second = IdempotencyCoordinator(redis, store)
    tokens = await asyncio.gather(
        first.claim(scope, biz_id, fingerprint="3" * 64),
        second.claim(scope, biz_id, fingerprint="3" * 64),
    )
    assert sorted(token is not None for token in tokens) == [False, True]
    other = IdempotencyCoordinator(redis, store)
    assert await other.claim(scope, biz_id, fingerprint="4" * 64) is None
    viewed = await first.inspect(scope, biz_id) or await second.inspect(scope, biz_id)
    assert viewed is not None
    assert viewed.fingerprint == "3" * 64


@pytest.mark.asyncio
async def test_owner_kill_then_new_instance_takeover(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
    tmp_path: Path,
) -> None:
    _engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"kill-{uuid4().hex[:12]}"
    fingerprint = "5" * 64
    ready = tmp_path / "ready"
    token_file = tmp_path / "token"
    env = os.environ.copy()
    env.update(
        {
            "SMS_CLAIM_READY": str(ready),
            "SMS_CLAIM_TOKEN": str(token_file),
            "SMS_CLAIM_SCOPE": scope.id,
            "SMS_CLAIM_BIZ": biz_id,
            "SMS_CLAIM_FP": fingerprint,
            "SMS_CLAIM_TTL": "3",
            "PYTHONPATH": str(_BACKEND_ROOT),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "from tests.integration.test_idempotency_claim_lease_postgres "
            "import child_hold_claim; child_hold_claim()"
        ),
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    for _ in range(50):
        if ready.exists():
            break
        await asyncio.sleep(0.1)
    assert ready.exists()
    for _ in range(50):
        if token_file.exists():
            break
        await asyncio.sleep(0.1)
    os.kill(process.pid, signal.SIGKILL)
    await process.wait()
    await asyncio.sleep(4)
    successor = IdempotencyCoordinator(redis, store, claim_ttl_s=3)
    token = await successor.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    viewed = await successor.inspect(scope, biz_id)
    assert viewed is not None
    assert viewed.generation >= 2


@pytest.mark.asyncio
async def test_mixed_old_api_set_nx_cannot_cover_new_generation(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    _engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = f"mix-{uuid4().hex[:12]}"
    fingerprint = "6" * 64
    coordinator = IdempotencyCoordinator(redis, store)
    token = await coordinator.claim(scope, biz_id, fingerprint=fingerprint)
    assert token is not None
    viewed = await coordinator.inspect(scope, biz_id)
    assert viewed is not None
    stale = f"{'7' * 32}:{fingerprint}:{max(1, viewed.generation - 1)}"
    claimed = await redis.set(coordinator.claim_key(scope, biz_id), stale, nx=True)
    assert not claimed
    await redis.set(coordinator.claim_key(scope, biz_id), stale)
    assert await coordinator.renew(scope, biz_id, token) is True
    raw = await redis.get(coordinator.claim_key(scope, biz_id))
    assert raw is not None
    restored = parse_claim_payload(raw)
    assert restored.token == token
    assert restored.generation == viewed.generation


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "limit", "cancel"])
async def test_pipeline_rejection_immediately_releases_authoritative_claim(
    claim_env: Any, stage: str
) -> None:
    from app.core.apikey import ApiAppContext
    from app.services.app_ratelimit import ApplicationRateLimitExceeded
    from app.services.pipeline import PipelineConfig, SendPipeline, SendRequest
    from app.services.send_admission import SendAdmissionRejected
    from tests.test_send_pipeline import FakeFrequency, FakePublisher, FakeQuota

    engine, store, redis, app_id = claim_env
    coordinator = IdempotencyCoordinator(redis, store)
    pipeline = SendPipeline(
        store=store,
        idempotency=coordinator,
        crypto=_crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    error = (
        asyncio.CancelledError()
        if stage == "cancel"
        else ApplicationRateLimitExceeded("limited")
        if stage == "limit"
        else SendAdmissionRejected("closed", "test", 30)
    )

    async def reject(*_args: Any) -> None:
        raise error

    if stage == "limit":
        pipeline._consume_request_limit = reject
    else:
        pipeline._authorize_new_send = reject
    biz = uuid4().hex
    scope = IdempotencyScope("app", str(app_id))
    with pytest.raises(type(error)) as raised:
        await pipeline.accept(
            ApiAppContext(app_id, "app", "平台部", frozenset({"notice"})),
            SendRequest("notice", (), content="test", biz_id=biz),
        )
    assert raised.value is error
    row = await _claim_row(engine, scope, biz)
    assert row["state"] == "released" and row["batch_id"] is None
    assert await redis.get(coordinator.claim_key(scope, biz)) is None
    assert await coordinator.claim(scope, biz, fingerprint="a" * 64) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_status", ["failed", "submitted", "unknown_terminal", "pending"])
async def test_r8_completed_retirement_helper_proves_lifecycle_before_new_generation(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int], chunk_status: str,
) -> None:
    from app.services.idempotency_lifecycle import reclaim_completed_result

    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = "r8-" + uuid4().hex[:16]
    coordinator = IdempotencyCoordinator(redis, store)
    token = await coordinator.claim(scope, biz_id, fingerprint="2" * 64)
    assert token is not None
    viewed = await coordinator.inspect(scope, biz_id)
    assert viewed is not None
    stored = await _complete(store, app_id=app_id, biz_id=biz_id, token=token,
                             generation=viewed.generation, fingerprint="2" * 64)
    async with engine.begin() as connection:
        batch_id = await connection.scalar(text("""
            UPDATE sms_batch SET status='completed' WHERE batch_no=:no RETURNING id
        """), {"no": stored.batch_no})
        await connection.execute(text("""
            INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
            VALUES(:id,1,:custom,1,:status)
        """), {"id": batch_id, "custom": uuid4().hex, "status": chunk_status})
        await connection.execute(text("""
            UPDATE idempotency_record SET expires_at=now()-interval '1 second'
            WHERE batch_id=:id
        """), {"id": batch_id})
    async with engine.begin() as connection:
        value = await reclaim_completed_result(connection, {
            "scope_kind": scope.kind, "scope_id": scope.id, "biz_id": biz_id,
            "token": uuid4().hex, "fingerprint": "3" * 64, "ttl_s": 30,
        }, batch_id)
    if chunk_status in {"failed", "submitted"}:
        assert value == viewed.generation + 1
        assert await store.find_existing(scope, biz_id) is None
        assert await store.renew_idempotency_claim(
            scope, biz_id, token=token, fingerprint="2" * 64,
            generation=viewed.generation, ttl_s=30,
        ) is False
    else:
        assert value is None
        assert await store.find_existing(scope, biz_id) == stored.batch_no


@pytest.mark.asyncio
async def test_r8_projection_repair_cannot_overwrite_newer_redis_generation(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    from app.services.idempotency import IdempotencyClaimView, claim_payload

    _, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = "r8-proj-" + uuid4().hex[:12]
    key = IdempotencyCoordinator.claim_key(scope, biz_id)
    authoritative = IdempotencyClaimView("a" * 32, "1" * 64, 1)
    future = claim_payload(IdempotencyClaimView("b" * 32, "2" * 64, 2))
    await redis.set(key, claim_payload(IdempotencyClaimView("c" * 32, "1" * 64, 1)))

    class InterleavedRedis:
        async def eval(self, *args: Any) -> Any:
            result = await redis.eval(*args)
            # 固定交错：Lua 已返回、Python 收到回复前，新代次完成投影。
            await redis.set(key, future)
            return result

        async def set(self, *args: Any, **kwargs: Any) -> Any:
            return await redis.set(*args, **kwargs)

    coordinator = IdempotencyCoordinator(InterleavedRedis(), store)
    await coordinator._project_view(scope, biz_id, authoritative)
    assert await redis.get(key) == future
    await redis.delete(key)


@pytest.mark.asyncio
async def test_r8_stale_result_cache_does_not_delete_concurrent_new_result(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    _, _, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz_id = "r8-cache-" + uuid4().hex[:12]
    key = IdempotencyCoordinator.key(scope, biz_id)
    old_result, new_result = uuid4().hex, uuid4().hex
    await redis.set(key, old_result)

    class Authority:
        async def exists(self, *args: Any) -> bool:
            assert args[-1] == old_result
            await redis.set(key, new_result)
            return False

        async def find_existing(self, *args: Any) -> str:
            return new_result

    coordinator = IdempotencyCoordinator(redis, Authority())
    await coordinator.lookup(scope, biz_id)
    assert await redis.get(key) == new_result
    await redis.delete(key)


async def _r8_expired_result(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int], *,
    batch_status: str = "completed", chunk_status: str = "failed",
    callback_status: str | None = None, expired: bool = True,
) -> tuple[IdempotencyScope, str, str, int, IdempotencyCoordinator, str]:
    engine, store, redis, app_id = claim_env
    scope = IdempotencyScope("app", str(app_id))
    biz = "r8-life-" + uuid4().hex[:12]
    owner = IdempotencyCoordinator(redis, store)
    token = await owner.claim(scope, biz, fingerprint="4" * 64)
    assert token is not None
    viewed = await owner.inspect(scope, biz)
    assert viewed is not None
    stored = await _complete(store, app_id=app_id, biz_id=biz, token=token,
                             generation=viewed.generation, fingerprint="4" * 64)
    async with engine.begin() as connection:
        batch_id = int(await connection.scalar(text("""
            UPDATE sms_batch SET status=:status WHERE batch_no=:no RETURNING id
        """), {"status": batch_status, "no": stored.batch_no}))
        await connection.execute(text("""
            INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
            VALUES(:id,1,:custom,1,:status)
        """), {"id": batch_id, "custom": uuid4().hex, "status": chunk_status})
        if expired:
            await connection.execute(text("""
                UPDATE idempotency_record SET expires_at=now()-interval '1 second'
                WHERE batch_id=:id
            """), {"id": batch_id})
        if callback_status is not None:
            await connection.execute(text("""
                INSERT INTO callback_task(app_id,event,batch_id,url,callback_secret_enc,
                                          callback_secret_key_version,status)
                VALUES(:app,'batch.finished',:batch,'http://127.0.0.1/cb',:secret,1,:status)
            """), {"app": app_id, "batch": batch_id, "secret": b"synthetic",
                    "status": callback_status})
    return scope, biz, stored.batch_no, batch_id, owner, token


async def _r8_cleanup_one(
    engine: Any, batch_id: int, *, after_selection: Any = None, fail_after_proof: bool = False,
) -> Any:
    from datetime import UTC, datetime

    from app.services.housekeeping import LifecyclePolicy
    from app.services.housekeeping_repository import SqlHousekeepingRepository

    async with engine.connect() as connection:
        identity = int(await connection.scalar(text(
            "SELECT id FROM idempotency_record WHERE batch_id=:id"
        ), {"id": batch_id}))
    runtime = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))

    @event.listens_for(runtime.sync_engine, "connect")
    def role(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("SET ROLE sms_send")
        cursor.close()

    class ConnectionProxy:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = await self.connection.execute(statement, *args, **kwargs)
            if "FOR UPDATE OF b SKIP LOCKED" in str(statement) and after_selection is not None:
                await after_selection()
            if "UPDATE idempotency_claim" in str(statement) and fail_after_proof:
                raise RuntimeError("synthetic rollback after proof")
            return result

    class EngineProxy:
        @asynccontextmanager
        async def begin(self) -> Any:
            async with runtime.begin() as connection:
                yield ConnectionProxy(connection)

        async def dispose(self) -> None:
            await runtime.dispose()

    repository = SqlHousekeepingRepository()
    repository._engine = lambda: EngineProxy()
    return await repository.cleanup_page(
        "idempotency", LifecyclePolicy(90, 90, 30), cutoff=datetime.now(UTC),
        cursor=(identity - 1,), limit=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch,chunk,callback,expired,protected", [
    ("completed", "failed", None, False, True),
    ("pending_approval", "failed", None, True, True),
    ("scheduled", "failed", None, True, True),
    ("queued", "failed", None, True, True),
    ("sending", "failed", None, True, True),
    ("balance_blocked", "failed", None, True, True),
    ("completed_unknown", "unknown_terminal", None, True, True),
    *[("completed", state, None, True, True) for state in (
        "pending", "submitting", "retrying", "uncertain", "unknown_terminal",
        "split_capacity_blocked", "failover_pending",
    )],
    ("completed", "failed", "pending", True, True),
    ("completed", "submitted", "retrying", True, True),
    ("completed", "failed", "done", True, False),
    ("completed", "failed", "dead", True, False),
    ("completed", "submitted", None, True, False),
    ("rejected", "failed", None, True, False),
    ("expired", "failed", None, True, False),
    ("cancelled", "failed", None, True, False),
])
async def test_r8_actual_cleanup_matches_all_lifecycle_readers(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
    batch: str, chunk: str, callback: str | None, expired: bool, protected: bool,
) -> None:
    engine, store, redis, _ = claim_env
    scope, biz, number, batch_id, coordinator, _ = await _r8_expired_result(
        claim_env, batch_status=batch, chunk_status=chunk,
        callback_status=callback, expired=expired,
    )
    assert await store.exists(scope, biz, number) is protected
    assert (await store.find_request_fingerprint(scope, biz) is not None) is protected
    await redis.set(coordinator.key(scope, biz), number)
    page = await _r8_cleanup_one(engine, batch_id)
    assert page.counts.idempotency == (0 if protected else 1)
    assert await coordinator.lookup(scope, biz) == (number if protected else None)
    assert await store.find_existing(scope, biz) == (number if protected else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleaned", [False, True])
async def test_r8_real_coordinator_reuses_expired_key_once_and_fences_old_owner(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int], cleaned: bool,
) -> None:
    engine, store, redis, _ = claim_env
    scope, biz, _, batch_id, original, old_token = await _r8_expired_result(claim_env)
    if cleaned:
        assert (await _r8_cleanup_one(engine, batch_id)).affected == 1
    first, second = IdempotencyCoordinator(redis, store), IdempotencyCoordinator(redis, store)
    tokens = await asyncio.gather(
        first.claim(scope, biz, fingerprint="5" * 64),
        second.claim(scope, biz, fingerprint="5" * 64),
    )
    assert sum(value is not None for value in tokens) == 1
    current = await first.inspect(scope, biz)
    assert current is not None and current.generation == 2
    await original.release(scope, biz, old_token)
    assert (await second.inspect(scope, biz)).token == current.token
    assert not await store.renew_idempotency_claim(
        scope, biz, token=old_token, fingerprint="4" * 64, generation=1, ttl_s=30,
    )


@pytest.mark.asyncio
async def test_r8_missing_historical_result_is_explicit_conflict_without_reclaim(
    claim_env: tuple[Any, SqlPipelineStore, Redis, int],
) -> None:
    from app.services.idempotency import IdempotencyConflict

    engine, store, redis, _ = claim_env
    scope, biz, _, batch_id, _, _ = await _r8_expired_result(claim_env)
    async with engine.begin() as connection:
        await connection.execute(text("""
            UPDATE idempotency_claim SET result_expires_at=NULL WHERE batch_id=:id
        """), {"id": batch_id})
        await connection.execute(text("DELETE FROM idempotency_record WHERE batch_id=:id"),
                                 {"id": batch_id})
    coordinator = IdempotencyCoordinator(redis, store)
    with pytest.raises(IdempotencyConflict, match="禁止自动重发"):
        await coordinator.inspect(scope, biz)
    with pytest.raises(IdempotencyConflict, match="禁止自动重发"):
        await coordinator.claim(scope, biz, fingerprint="6" * 64)
    assert (await _claim_row(engine, scope, biz))["generation"] == 1


@pytest.mark.parametrize("mutation", [
    "callback_insert", "callback_retry", "callback_move", "chunk_retry",
])
async def test_r8_cleanup_lock_serializes_late_protected_work(
    claim_env: Any, mutation: str,
) -> None:
    from app.services.idempotency import IdempotencyConflict

    engine, store, redis, app_id = claim_env
    scope, biz, _, batch_id, _, _ = await _r8_expired_result(
        claim_env, callback_status="dead" if mutation == "callback_retry" else None,
    )
    moved_task: int | None = None
    if mutation == "callback_move":
        async with engine.begin() as connection:
            moved_task = await connection.scalar(text("""
                INSERT INTO callback_task(app_id,event,url,callback_secret_enc,
                                          callback_secret_key_version,status)
                VALUES(:app,'batch.finished','http://127.0.0.1/cb',:secret,1,'pending')
                RETURNING id
            """), {"app": app_id, "secret": b"synthetic"})
    selected, resume, writer_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer_pid: list[int] = []

    async def after_selection() -> None:
        selected.set()
        await resume.wait()

    async def writer() -> None:
        async with engine.begin() as connection:
            if mutation.startswith("callback"):
                await connection.execute(text("SET LOCAL ROLE sms_callback"))
            writer_pid.append(await connection.scalar(text("SELECT pg_backend_pid()")))
            writer_ready.set()
            if mutation == "callback_insert":
                await connection.execute(text("""
                    INSERT INTO callback_task(app_id,event,batch_id,url,callback_secret_enc,
                                              callback_secret_key_version,status)
                    VALUES(:app,'batch.finished',:id,'http://127.0.0.1/cb',:secret,1,'pending')
                """), {"app": app_id, "id": batch_id, "secret": b"synthetic"})
            elif mutation == "callback_move":
                await connection.execute(text(
                    "UPDATE callback_task SET batch_id=:batch WHERE id=:task"
                ), {"batch": batch_id, "task": moved_task})
            else:
                table = "callback_task" if mutation == "callback_retry" else "sms_chunk"
                await connection.execute(text(
                    f"UPDATE {table} SET status='pending' WHERE batch_id=:id"
                ), {"id": batch_id})

    cleanup = asyncio.create_task(
        _r8_cleanup_one(engine, batch_id, after_selection=after_selection)
    )
    writer_task: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(selected.wait(), 3)
        writer_task = asyncio.create_task(writer())
        await asyncio.wait_for(writer_ready.wait(), 3)
        async with asyncio.timeout(3), engine.connect() as observer:
            while not await observer.scalar(text(
                "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
            ), {"pid": writer_pid[0]}):
                assert not writer_task.done()
                await asyncio.sleep(0.01)
        resume.set()
        assert (await cleanup).affected == 1
        await writer_task
        # 清理先提交后才建立的新保护状态：丢失原映射不得猜测重发。
        coordinator = IdempotencyCoordinator(redis, store)
        with pytest.raises(IdempotencyConflict):
            await coordinator.claim(scope, biz, fingerprint="7" * 64)
        assert (await _claim_row(engine, scope, biz))["generation"] == 1
    finally:
        resume.set()
        await asyncio.gather(
            cleanup, *([writer_task] if writer_task else []), return_exceptions=True
        )


async def test_r8_protected_writer_first_skips_locked_batch_then_keeps_result(
    claim_env: Any,
) -> None:
    engine, store, _, _ = claim_env
    scope, biz, number, batch_id, _, _ = await _r8_expired_result(claim_env, callback_status="dead")
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE callback_task SET status='pending' WHERE batch_id=:id"
        ), {"id": batch_id})
        async with asyncio.timeout(2):
            assert (await _r8_cleanup_one(engine, batch_id)).affected == 0
    assert (await _r8_cleanup_one(engine, batch_id)).affected == 0
    assert await store.find_existing(scope, biz) == number


async def test_r8_cleanup_proof_and_delete_rollback_together(claim_env: Any) -> None:
    engine, _, _, _ = claim_env
    _, _, _, batch_id, _, _ = await _r8_expired_result(claim_env)
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE idempotency_claim SET result_expires_at=NULL WHERE batch_id=:id"
        ), {"id": batch_id})
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        await _r8_cleanup_one(engine, batch_id, fail_after_proof=True)
    async with engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM idempotency_record WHERE batch_id=:id"
        ), {"id": batch_id}) == 1
        assert await connection.scalar(text(
            "SELECT result_expires_at FROM idempotency_claim WHERE batch_id=:id"
        ), {"id": batch_id}) is None


def _r8_pipeline(store: Any, redis: Any) -> Any:
    from app.services.pipeline import PipelineConfig, SendPipeline
    from tests.test_send_pipeline import FakeFrequency, FakePublisher, FakeQuota

    return SendPipeline(
        store=store, idempotency=IdempotencyCoordinator(redis, store), crypto=_crypto(),
        frequency=FakeFrequency(), quota=FakeQuota(), publisher=FakePublisher(),
        config=PipelineConfig(),
    )


@pytest.mark.parametrize("protected,cleaned", [(True, False), (False, False), (False, True)])
async def test_r8_actual_pipeline_cleanup_replay_and_new_acceptance(
    claim_env: Any, protected: bool, cleaned: bool,
) -> None:
    from app.core.apikey import ApiAppContext
    from app.services.pipeline import SendRequest

    engine, store, redis, app_id = claim_env
    scope, biz, number, batch_id, _, _ = await _r8_expired_result(
        claim_env, chunk_status="unknown_terminal" if protected else "failed",
    )
    pipeline = _r8_pipeline(store, redis)
    app = ApiAppContext(app_id, "app", "平台部", frozenset({"notice"}), blacklist_check=False)
    principal = ApplicationPrincipal(app_id, "app", "平台部")
    request = SendRequest("notice", ["13800138000"], content="合成通知", biz_id=biz,
                          actor=principal)
    fingerprint = pipeline._request_hash(request, app, pipeline._resolve_policy(app, request, None))
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE idempotency_record SET request_hash=:fp WHERE batch_id=:id"
        ), {"fp": fingerprint, "id": batch_id})
        await connection.execute(text(
            "UPDATE idempotency_claim SET fingerprint=:fp WHERE batch_id=:id"
        ), {"fp": fingerprint, "id": batch_id})
    if protected or cleaned:
        assert (await _r8_cleanup_one(engine, batch_id)).affected == (0 if protected else 1)
    # 此次检验始终穿过真实 Coordinator、数据库 Store 和 Pipeline。
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        response = await pipeline.accept(app, request)
    assert response.idempotent is protected
    assert (response.batch_no == number) is protected
    replay = await pipeline.accept(app, request)
    assert replay.idempotent and replay.batch_no == response.batch_no
    async with engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_batch WHERE app_id=:app AND biz_id=:biz"
        ), {"app": app_id, "biz": biz}) == (1 if protected else 2)
        assert await connection.scalar(text("""
            SELECT count(*) FROM outbox_event e JOIN sms_batch b ON b.batch_no=e.aggregate_id
            WHERE b.app_id=:app AND b.biz_id=:biz AND e.event_type='batch.ready'
        """), {"app": app_id, "biz": biz}) == (1 if protected else 2)
    assert (await _claim_row(engine, scope, biz))["generation"] == (1 if protected else 2)


@pytest.mark.parametrize("route", ["send", "uat-send"])
@pytest.mark.parametrize("failure", ["orphan", "database", "redis"])
async def test_r8_actual_asgi_preflight_has_stable_conflict_or_unavailable(
    claim_env: Any, monkeypatch: pytest.MonkeyPatch, route: str, failure: str,
) -> None:
    from httpx import ASGITransport, AsyncClient

    import app.api.messages as messages_module
    from app.core.apikey import ApiAppContext, get_api_key_authenticator
    from tests.test_messages_api import make_app

    engine, store, redis, app_id = claim_env
    scope, biz, _, batch_id, _, _ = await _r8_expired_result(claim_env)
    if failure == "orphan":
        async with engine.begin() as connection:
            await connection.execute(text(
                "UPDATE idempotency_claim SET result_expires_at=NULL WHERE batch_id=:id"
            ), {"id": batch_id})
            await connection.execute(text(
                "DELETE FROM idempotency_record WHERE batch_id=:id"
            ), {"id": batch_id})
    elif failure == "database":
        async def down(*_args: Any) -> Any:
            raise OSError("synthetic dependency detail must not escape")
        monkeypatch.setattr(store, "find_existing", down)
    else:
        class RedisDown:
            async def get(self, *_args: Any) -> Any:
                raise OSError("synthetic dependency detail must not escape")
        redis = RedisDown()
    pipeline = _r8_pipeline(store, redis)

    async def factory(_app: Any) -> Any:
        return pipeline

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preflight must not enter new-send policy, provider, quota or UAT control")

    class Auth:
        async def authenticate(self, _key: str) -> Any:
            return ApiAppContext(app_id, "app", "平台部", frozenset({"notice"}))

    pipeline._authorize_new_send = forbidden
    monkeypatch.setattr(messages_module, "_pipeline", factory)
    monkeypatch.setattr(messages_module, "_require_vendor_test_api_ready", forbidden)
    api = make_app()
    api.dependency_overrides[get_api_key_authenticator] = Auth
    async with AsyncClient(transport=ASGITransport(api), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/messages/{route}", headers={"X-Api-Key": "synthetic"},
                                     json={"category": "notice", "mobiles": ["13800138000"],
                                           "content": "合成通知", "biz_id": biz})
    assert response.status_code == (409 if failure == "orphan" else 503), response.text
    assert set(response.json()) == {"code", "message", "detail"}
    assert "synthetic dependency detail" not in response.text
    assert (await _claim_row(engine, scope, biz))["generation"] == 1
    async with engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM sms_batch WHERE app_id=:app AND biz_id=:biz"
        ), {"app": app_id, "biz": biz}) == 1


async def test_r8_callback_runtime_role_can_lock_and_retry_without_batch_write(
    claim_env: Any,
) -> None:
    from app.services.callback_repository import SqlCallbackRepository
    from tests.integration.test_ops_audit_postgres import _create_admin

    engine, _, _, app_id = claim_env
    _, _, _, batch_id, _, _ = await _r8_expired_result(claim_env, callback_status="dead")
    async with engine.begin() as connection:
        await connection.execute(text("""
            UPDATE app SET callback_url='http://127.0.0.1/cb',callback_secret_enc=:secret
            WHERE id=:id
        """), {"id": app_id, "secret": b"synthetic"})
        task_id = await connection.scalar(text(
            "SELECT id FROM callback_task WHERE batch_id=:id"
        ), {"id": batch_id})
    runtime = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))

    @event.listens_for(runtime.sync_engine, "connect")
    def role(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("SET ROLE sms_callback")
        cursor.close()

    event.listen(runtime.sync_engine, "begin", _set_audit_transaction_context)
    repository = SqlCallbackRepository()
    repository._engine = lambda: runtime
    principal = await _create_admin(engine, login="synthetic-admin-" + uuid4().hex[:8])
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        await repository.manual_retry(task_id, principal=principal)
    async with engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT status FROM callback_task WHERE id=:id"
        ), {"id": task_id}) == "pending"
        assert not await connection.scalar(text(
            "SELECT has_any_column_privilege('sms_callback','sms_batch','UPDATE')"
        ))


@pytest.mark.parametrize("missing", ["chunk", "fingerprint"])
async def test_r8_missing_result_facts_are_conservatively_retained(
    claim_env: Any, missing: str,
) -> None:
    engine, store, _, _ = claim_env
    scope, biz, number, batch_id, _, _ = await _r8_expired_result(claim_env)
    async with engine.begin() as connection:
        if missing == "chunk":
            await connection.execute(text("DELETE FROM sms_chunk WHERE batch_id=:id"),
                                     {"id": batch_id})
        else:
            await connection.execute(text("""
                UPDATE idempotency_record SET request_hash=NULL,request_hash_key_version=NULL
                WHERE batch_id=:id
            """), {"id": batch_id})
    assert (await _r8_cleanup_one(engine, batch_id)).affected == 0
    assert await store.exists(scope, biz, number)
