from __future__ import annotations

import asyncio
import base64
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import ApplicationPrincipal
from app.core.runtime_resources import bind_connection_system_audit
from app.services.app_ratelimit import ControlPlaneUnavailable
from app.services.crypto import CryptoService
from app.services.idempotency import IdempotencyCoordinator, IdempotencyScope, parse_claim_payload
from app.services.pipeline import BatchCommand
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


def _store(database_url: Any) -> SqlPipelineStore:
    return SqlPipelineStore(settings=cast(Any, SimpleNamespace(database_url=database_url)))


def child_hold_claim() -> None:
    """独立进程占用 Claim，供父进程 SIGKILL 后验证接管。"""

    async def _claim() -> str:
        redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
        store = _store(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
        coordinator = IdempotencyCoordinator(
            redis,
            store,
            claim_ttl_s=int(os.environ["SMS_CLAIM_TTL"]),
        )
        scope = IdempotencyScope("app", os.environ["SMS_CLAIM_SCOPE"])
        token = await coordinator.claim(
            scope,
            os.environ["SMS_CLAIM_BIZ"],
            fingerprint=os.environ["SMS_CLAIM_FP"],
        )
        if token is None:
            raise AssertionError("child must own the claim")
        return token

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
    store = _store(database_url)
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    try:
        yield engine, store, redis, app_id
    finally:
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
    stored = await store.save(
        _command(
            app_id=app_id,
            biz_id=biz_id,
            token=token,
            generation=viewed.generation,
            fingerprint=fingerprint,
        )
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
            stored = await store.save(
                _command(
                    app_id=app_id,
                    biz_id=biz_id,
                    token=token,
                    generation=viewed.generation,
                    fingerprint=fingerprint,
                )
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
    stored = await store.save(
        _command(
            app_id=app_id,
            biz_id=done_biz,
            token=done_token,
            generation=done_view.generation,
            fingerprint=fingerprint,
        )
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
    stored = await store.save(
        _command(
            app_id=app_id,
            biz_id=biz_id,
            token=token2,
            generation=new_view.generation,
            fingerprint=fingerprint,
        )
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
    stored = await store.save(
        _command(
            app_id=app_id,
            biz_id=biz_id,
            token=token,
            generation=viewed.generation,
            fingerprint=fingerprint,
        )
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
