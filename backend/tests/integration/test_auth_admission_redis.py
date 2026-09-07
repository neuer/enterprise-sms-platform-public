"""仅隔离 Redis 7：执行实际 Lua，覆盖多实例守恒、取消和损坏状态。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.auth.admission import AdmissionBusy, LoginAdmission
from app.core.auth.admission_policy import (
    ACTIVE_KEY,
    POLICY_KEY,
    WORK_KEY,
    AdmissionLimits,
    AdmissionPolicy,
    AdmissionPolicyRuntime,
)
from app.core.auth.backends import AuthenticatedIdentity, SessionStateUnavailable
from app.core.auth.service import AuthService, LoginGuard, RedisKeyValue
from app.core.bounded_executor import BoundedExecutor, BoundedWorkScope
from app.settings import Settings
from tests.integration.test_auth_guard_redis import PrefixedRedisKeyValue
from tests.test_auth_admission_policy import approvals

pytestmark = pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="isolated Redis 7")
IP = "192.0.2.9"


@pytest.fixture
async def admission() -> AsyncIterator[tuple[Any, ...]]:
    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    other = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    prefix = "auth-admission-test:" + uuid4().hex
    store = PrefixedRedisKeyValue(RedisKeyValue(client), prefix)
    peer = PrefixedRedisKeyValue(RedisKeyValue(other), prefix)
    policy = [
        AdmissionPolicy(1, AdmissionLimits(global_burst=16, global_refill_ms=100), approvals())
    ]

    async def load() -> AdmissionPolicy:
        return policy[0]

    runtime = AdmissionPolicyRuntime(Settings(), store, loader=load)
    await runtime.ensure_ready()
    # 测试时钟只回拨工作桶的历史基准，避免每个隔离夹具等一次自然恢复。
    second, micro = await client.time()
    await client.hset(store._key(WORK_KEY), "updated_ms", second * 1000 + micro // 1000 - 2000)
    first = LoginAdmission(store, runtime.load, key=b"synthetic-key-for-auth-sources-32-bytes")
    second_guard = LoginAdmission(peer, runtime.load, key=first.key)
    try:
        yield first, second_guard, client, store, runtime, policy
    finally:
        await runtime.stop()
        if first._finishing:
            await asyncio.gather(*first._finishing, return_exceptions=True)
        keys = store.keys | peer.keys
        if keys:
            await client.delete(*keys)
        await client.aclose()
        await other.aclose()


async def test_fifty_shared_users_finish_with_bounded_retry_and_actual_provider_boundary(
    admission: tuple[Any, ...],
) -> None:
    first, peer, client, store, _runtime, _policy = admission
    active = 0
    peak = 0
    verified = 0

    class Provider:
        async def authenticate(self, code: str, name: str, _password: str, **_kw: Any) -> Any:
            nonlocal active, peak, verified
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            verified += 1
            return AuthenticatedIdentity(code, name, name, name, "test", ())

    services = [
        AuthService(Provider(), LoginGuard(item.store, admission=item)) for item in (first, peer)
    ]

    async def login(index: int) -> None:
        service = services[index % 2]
        async with asyncio.timeout(15):
            while True:
                try:
                    identity = await service.authenticate("local", f"synthetic-{index}", "test", IP)
                    # 代替权威用户库的本测试控制：每个稳定身份均被合成账号绑定接受。
                    await service.record_bound_success(identity.login_name)
                    await service.record_completed_success(identity, IP)
                    return
                except AdmissionBusy:
                    await asyncio.sleep(0.05)

    await asyncio.gather(*(login(index) for index in range(50)))
    assert verified == 50 and peak <= 2
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1
    window_keys = [key for key in store.keys if key.endswith(":window") and "admission" in key]
    assert len(window_keys) == 1
    assert await client.hget(window_keys[0], "count") == "0"


async def test_refund_single_use_does_not_return_work_or_new_window(
    admission: tuple[Any, ...],
) -> None:
    first, peer, client, store, runtime, policy = admission
    reservation = await first.admit("local", IP)
    await first.release_work(reservation, BoundedWorkScope())
    work_before = await client.hgetall(store._key(WORK_KEY))
    await asyncio.gather(first.complete(reservation, IP), peer.complete(reservation, IP))
    assert await client.hgetall(store._key(WORK_KEY)) == work_before
    assert float(await client.hget(store._key(reservation.keys[0]), "tokens")) <= 100
    assert await client.hget(store._key(reservation.keys[1]), "count") == "0"
    assert await client.exists(store._key(reservation.keys[2])) == 0

    old = await first.admit("local", IP)
    await first.release_work(old, BoundedWorkScope())
    policy[0] = replace(policy[0], revision=2)
    await runtime.ensure_ready()
    before = await client.hgetall(store._key(old.keys[0]))
    await first.complete(old, IP)
    assert await client.hgetall(store._key(old.keys[0])) == before


async def test_public_success_keeps_strict_five_burst(admission: tuple[Any, ...]) -> None:
    first, _peer, _client, _store, _runtime, _policy = admission
    for _ in range(5):
        item = await first.admit("local", "198.51.100.1")
        await first.release_work(item, BoundedWorkScope())
        await first.complete(item, "198.51.100.1")
    with pytest.raises(AdmissionBusy):
        await first.admit("local", "198.51.100.1")


async def test_policy_mismatch_and_missing_work_fail_closed(admission: tuple[Any, ...]) -> None:
    first, _peer, client, store, runtime, _policy = admission
    await client.hset(store._key(POLICY_KEY), "digest", "unknown")
    with pytest.raises(SessionStateUnavailable):
        await first.admit("local", IP)
    with pytest.raises(SessionStateUnavailable):
        await runtime.ensure_ready()
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1


async def test_global_budget_and_concurrency_cannot_be_refunded(admission: tuple[Any, ...]) -> None:
    first, peer, client, store, _runtime, _policy = admission
    a, b = await asyncio.gather(first.admit("local", IP), peer.admit("local", IP))
    with pytest.raises(AdmissionBusy):
        await first.admit("local", IP)
    await first.release_work(a, BoundedWorkScope())
    await peer.release_work(b, BoundedWorkScope())
    await client.hset(store._key(WORK_KEY), "tokens", 0)
    second, micro = await client.time()
    await client.hset(store._key(WORK_KEY), "updated_ms", second * 1000 + micro // 1000)
    await asyncio.gather(first.complete(a, IP), peer.complete(b, IP))
    with pytest.raises(AdmissionBusy):
        await first.admit("local", IP)


async def test_timeout_retains_actual_thread_slot_and_releases_after_finish(
    admission: tuple[Any, ...],
) -> None:
    first, _peer, client, store, _runtime, _policy = admission
    release = Event()
    executor = BoundedExecutor(max_workers=1, max_pending=1)

    class Provider:
        async def authenticate(self, *_args: Any, **_kw: Any) -> Any:
            await executor.run(lambda: release.wait(5), timeout_s=0.02)

    service = AuthService(Provider(), LoginGuard(first.store, admission=first))
    try:
        with pytest.raises(TimeoutError):
            await service.authenticate("local", "test", "test", IP)
        assert await client.hlen(store._key(ACTIVE_KEY)) == 2
        release.set()
        await asyncio.gather(*first._finishing)
        assert await client.hlen(store._key(ACTIVE_KEY)) == 1
    finally:
        release.set()
        executor.close()


async def test_abandoned_expired_work_is_unknown_not_empty(admission: tuple[Any, ...]) -> None:
    first, _peer, client, store, _runtime, _policy = admission
    reservation = await first.admit("local", IP)
    await client.hset(
        store._key(ACTIVE_KEY), reservation.request_id, reservation.source + ":local:1"
    )
    with pytest.raises(SessionStateUnavailable):
        await first.admit("ad", IP)
    await first.release_work(reservation, BoundedWorkScope())


async def test_reauthentication_does_not_consume_interactive_login_pool(
    admission: tuple[Any, ...],
) -> None:
    first, _peer, client, store, _runtime, _policy = admission
    release = Event()
    ready = asyncio.Event()
    reauth_pool = BoundedExecutor(max_workers=1, max_pending=2)
    login_pool = BoundedExecutor(max_workers=1, max_pending=2)
    started = 0

    class Provider:
        async def authenticate(
            self, code: str, name: str, _password: str, *, purpose: str,
        ) -> Any:
            nonlocal started
            if purpose == "reauthentication":
                started += 1
                if started == 2:
                    ready.set()
                await reauth_pool.run(lambda: release.wait(5), timeout_s=6)
            else:
                await login_pool.run(lambda: True, timeout_s=1)
            return AuthenticatedIdentity(code, name, name, name, "test", ())

    service = AuthService(Provider(), LoginGuard(first.store, admission=first))
    tasks = [
        asyncio.create_task(
            service.authenticate(
                "local", f"reauth-{i}", "test", f"198.51.100.{i + 1}",
                purpose="reauthentication",
            )
        ) for i in range(2)
    ]
    try:
        await asyncio.wait_for(ready.wait(), 2)
        identity = await service.authenticate("local", "login", "test", IP)
        assert identity.login_name == "login" and not release.is_set()
        assert await client.hlen(store._key(ACTIVE_KEY)) == 3
    finally:
        release.set()
        await asyncio.gather(*tasks)
        reauth_pool.close()
        login_pool.close()


async def test_published_process_does_not_reinitialize_lost_control_keys(
    admission: tuple[Any, ...],
) -> None:
    first, _peer, client, store, runtime, _policy = admission
    await client.delete(*(store._key(key) for key in (POLICY_KEY, WORK_KEY, ACTIVE_KEY)))
    for _ in range(2):
        with pytest.raises(SessionStateUnavailable):
            await runtime.ensure_ready()
        with pytest.raises(SessionStateUnavailable):
            await first.admit("local", IP)
    assert await client.exists(store._key(WORK_KEY)) == 0


async def test_incomplete_source_generation_cannot_settle_success(
    admission: tuple[Any, ...],
) -> None:
    first, _peer, client, store, _runtime, _policy = admission
    item = await first.admit("local", IP)
    await first.release_work(item, BoundedWorkScope())
    await client.hdel(store._key(item.keys[0]), "generation")
    with pytest.raises(SessionStateUnavailable):
        await first.complete(item, IP)
    assert await client.exists(store._key(item.keys[2])) == 1
