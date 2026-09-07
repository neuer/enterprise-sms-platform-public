"""受理续租与幂等指纹优化必须运行真实 Coordinator 和 Pipeline。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.idempotency import (
    CLAIM_PROJECT_LUA,
    CLAIM_RELEASE_LUA,
    CLAIM_RENEW_LUA,
    IdempotencyCoordinator,
    IdempotencyFingerprint,
    IdempotencyScope,
)
from app.services.pipeline import ApiAppContext, PipelineConfig, SendPipeline, SendRequest
from tests.test_send_pipeline import (
    FakeFrequency,
    FakeIdempotency,
    FakePublisher,
    FakeQuota,
    FakeStore,
    FakeUsageLedger,
    crypto,
)


class ClaimRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.renewals = 0

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, _numkeys: int, key: str, payload: str, *args: Any) -> int:
        if script == CLAIM_PROJECT_LUA:
            self.values[key] = payload
            return 1
        if self.values.get(key) != payload:
            return 0
        if script == CLAIM_RENEW_LUA:
            self.renewals += 1
        elif script == CLAIM_RELEASE_LUA:
            self.values.pop(key)
        else:
            raise AssertionError("unexpected script")
        return 1

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class ClaimStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0
        self.owned = True
        self.generation = 4

    async def reserve_idempotency_claim(self, *args: Any, **kwargs: Any) -> int:
        return self.generation

    async def renew_idempotency_claim(self, *args: Any, **kwargs: Any) -> bool:
        self.renewals += 1
        await asyncio.sleep(0)
        return self.owned and kwargs["generation"] == self.generation

    async def find_existing(self, *args: Any) -> None:
        return None

    async def exists(self, *args: Any) -> bool:
        return False


def pipeline_for(idempotency: Any, *, store: Any = None, ledger: Any = None) -> SendPipeline:
    return SendPipeline(
        store=store or FakeStore(),
        idempotency=idempotency,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("category,expected_calls", [("notice", 0), ("verify", 3), ("market", 3)])
async def test_pipeline_collapses_short_request_renewals_with_real_coordinator(
    category: str, expected_calls: int
) -> None:
    class Ledger(FakeUsageLedger):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def allow_frequency_many(self, *args: Any, **kwargs: Any) -> list[bool]:
            self.calls += 1
            return await super().allow_frequency_many(*args, **kwargs)

    store, redis, ledger = ClaimStore(), ClaimRedis(), Ledger()
    coordinator = IdempotencyCoordinator(redis, store, clock=lambda: 0.0)
    pipeline = pipeline_for(coordinator, store=store, ledger=ledger)
    await pipeline.accept(
        ApiAppContext(1, "app", "研发部", frozenset({category}), allow_market_api_bulk=True),
        SendRequest(
            category,
            [f"1380013{index:04d}" for index in range(401)],
            content="消息",
            biz_id="bounded-renew",
        ),
    )
    assert ledger.calls == expected_calls
    assert store.renewals == redis.renewals == 1
    assert len(store.commands) == 1
    assert coordinator._renewed_until == {}


@pytest.mark.asyncio
async def test_renewal_clock_coalesces_concurrent_checks_and_rejects_new_owner() -> None:
    now = [0.0]
    store, redis = ClaimStore(), ClaimRedis()
    coordinator = IdempotencyCoordinator(redis, store, clock=lambda: now[0])
    scope = IdempotencyScope("app", "1")
    token = await coordinator.claim(scope, "renew", fingerprint="f" * 64)
    assert token is not None
    assert all(
        await asyncio.gather(*(coordinator.renew_if_due(scope, "renew", token) for _ in range(50)))
    )
    assert store.renewals == redis.renewals == 1
    now[0] = 10
    assert await coordinator.renew_if_due(scope, "renew", token)
    assert store.renewals == redis.renewals == 2
    now[0] = 20
    store.generation += 1
    assert not await coordinator.renew_if_due(scope, "renew", token)
    assert coordinator._renewed_until == {}
    assert store.renewals == 3 and redis.renewals == 2


@pytest.mark.asyncio
async def test_heartbeat_and_foreground_share_deadline_and_keep_long_operation_alive() -> None:
    now = [0.0]
    wake, slept = asyncio.Queue[float](), asyncio.Event()

    async def sleeper(_seconds: float) -> None:
        slept.set()
        now[0] = await wake.get()

    store, redis = ClaimStore(), ClaimRedis()
    coordinator = IdempotencyCoordinator(redis, store, clock=lambda: now[0], sleeper=sleeper)
    scope = IdempotencyScope("app", "1")
    token = await coordinator.claim(scope, "heartbeat", fingerprint="f" * 64)
    assert token is not None
    assert await coordinator.renew_if_due(scope, "heartbeat", token)
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(coordinator.heartbeat(scope, "heartbeat", token, lost))
    await slept.wait()
    for instant in (10.0, 20.0, 40.0):
        slept.clear()
        await wake.put(instant)
        await slept.wait()
        before = store.renewals
        assert await coordinator.renew_if_due(scope, "heartbeat", token)
        assert store.renewals == before
    assert store.renewals == 4
    store.owned = False
    await wake.put(50.0)
    await asyncio.wait_for(heartbeat, timeout=1)
    assert lost.is_set()
    assert not await coordinator.renew_if_due(scope, "heartbeat", token)


@pytest.mark.asyncio
async def test_normalized_replay_reuses_computed_fingerprint_and_skips_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))
    request = SendRequest("notice", ["13900139000", "13800138000"], content="通知", biz_id="fp")
    pipeline = pipeline_for(FakeIdempotency())
    policy = pipeline._resolve_policy(app, request, None)
    digest = pipeline._request_hash(request, app, policy, key_version=1)
    pipeline.idempotency = FakeIdempotency("existing", stored_request_hash=digest)
    original = pipeline._request_hash
    calls: list[bool] = []

    def record(*args: Any, **kwargs: Any) -> str:
        calls.append(kwargs.get("normalize", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_request_hash", record)
    result = await pipeline.accept(app, request)
    assert result.idempotent
    assert calls == [True]


@pytest.mark.asyncio
async def test_cached_fingerprint_does_not_cross_key_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = pipeline_for(FakeIdempotency("existing", stored_request_hash="different"))
    app = ApiAppContext(1, "app", "研发部", frozenset({"notice"}))
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="fp")
    versions: list[int] = []

    def compute(*args: Any, **kwargs: Any) -> str:
        versions.append(kwargs["key_version"])
        return "different"

    monkeypatch.setattr(pipeline, "_request_hash", compute)
    await pipeline._ensure_same_request(
        IdempotencyScope("app", "1"),
        "fp",
        request,
        app,
        pipeline._resolve_policy(app, request, None),
        computed=IdempotencyFingerprint("different", 2),
    )
    assert versions == [1]
