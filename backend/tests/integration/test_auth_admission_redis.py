"""仅隔离 Redis 7：执行实际 Lua，覆盖多实例守恒、取消和损坏状态。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress
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
    assert await client.hget(store._key(reservation.keys[2]), "state") == "settled"

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
    # 合成服务器时钟推进：Request 与 Active 的同一原始所有权值一起到期。
    await client.hset(
        store._key(reservation.keys[2]), "active_value", reservation.source + ":local:1"
    )
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


async def test_r8_auth_admit_reply_loss_keeps_owned_cleanup(
    admission: tuple[Any, ...],
) -> None:
    from app.core.auth.admission import ADMIT_LUA, drain_login_admissions

    guard, _, client, store, _, _ = admission

    class LostReply:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        async def eval(self, script: str, *args: Any) -> Any:
            result = await store.eval(script, *args)
            if script == ADMIT_LUA:
                raise SessionStateUnavailable("synthetic committed reply loss")
            return result

    guard.store = LostReply()
    with pytest.raises(SessionStateUnavailable):
        await guard.admit("local", IP)
    await drain_login_admissions()
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1


async def test_r8_auth_release_failure_retains_recovery_responsibility(
    admission: tuple[Any, ...],
) -> None:
    from app.core.auth.admission import RELEASE_LUA, drain_login_admissions

    guard, _, client, store, _, _ = admission
    reservation = await guard.admit("local", IP)

    class FailOnce:
        failed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        async def eval(self, script: str, *args: Any) -> Any:
            if script == RELEASE_LUA and not self.failed:
                self.failed = True
                raise SessionStateUnavailable("synthetic release before execution failure")
            return await store.eval(script, *args)

    guard.store = FailOnce()
    with suppress(SessionStateUnavailable):
        await guard.release_work(reservation, BoundedWorkScope())
    await drain_login_admissions()
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1


@pytest.mark.parametrize("executed", [False, True])
async def test_r8_auth_unknown_admit_fences_late_replay_without_second_charge(
    admission: tuple[Any, ...], executed: bool,
) -> None:
    from app.core.auth.admission import ADMIT_LUA, drain_login_admissions

    guard, peer, client, store, _, _ = admission
    captured: list[Any] = []

    class LoseAdmission:
        async def eval(self, script: str, *args: Any) -> Any:
            if script != ADMIT_LUA:
                return await store.eval(script, *args)
            captured.extend(args)
            if executed:
                await store.eval(script, *args)
            raise SessionStateUnavailable("synthetic unknown admission")

    guard.store = LoseAdmission()
    with pytest.raises(SessionStateUnavailable):
        await guard.admit("local", IP)
    await drain_login_admissions()
    before = await client.hgetall(store._key(WORK_KEY))
    assert await store.eval(ADMIT_LUA, *captured) == [2, 0]
    assert await client.hgetall(store._key(WORK_KEY)) == before
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1
    assert not guard._owners
    # 新实例的正常请求仍可进入，恢复不重放 Provider。
    peer = LoginAdmission(store, peer.policy_loader, key=peer.key)
    item = await peer.admit("local", IP)
    await peer.release_work(item, BoundedWorkScope())


async def test_r8_auth_original_command_age_rejects_after_terminal_expiry(
    admission: tuple[Any, ...],
) -> None:
    from app.core.auth.admission import ADMIT_LUA, TIME_LUA, drain_login_admissions

    guard, _, client, store, _, _ = admission
    captured: list[Any] = []

    class OldOriginalCommand:
        async def eval(self, script: str, *args: Any) -> Any:
            result = await store.eval(script, *args) if script != ADMIT_LUA else None
            if script == TIME_LUA:
                return [int(result[0]) - 601, result[1]]
            if script == ADMIT_LUA:
                captured.extend(args)
                raise SessionStateUnavailable("synthetic delayed original command")
            return result

    guard.store = OldOriginalCommand()
    with pytest.raises(SessionStateUnavailable):
        await guard.admit("local", IP)
    await drain_login_admissions()
    request_key = captured[6]
    # 仅删除本合成已终结键，模拟其 600 秒 TTL 已耗尽；原发令时间不变。
    await client.delete(store._key(request_key))
    before = await client.hgetall(store._key(WORK_KEY))
    assert await store.eval(ADMIT_LUA, *captured) == [-1, 0]
    assert await client.hgetall(store._key(WORK_KEY)) == before
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1


async def test_r8_auth_admit_retry_and_release_reply_loss_are_single_use(
    admission: tuple[Any, ...],
) -> None:
    from app.core.auth.admission import ADMIT_LUA, RELEASE_LUA

    guard, _, client, store, _, _ = admission
    captured: list[Any] = []

    class DuplicateAndLostRelease:
        lost = False

        async def eval(self, script: str, *args: Any) -> Any:
            result = await store.eval(script, *args)
            if script == ADMIT_LUA:
                captured.extend(args)
            if script == RELEASE_LUA and not self.lost:
                self.lost = True
                raise SessionStateUnavailable("synthetic release committed reply loss")
            return result

    guard.store = DuplicateAndLostRelease()
    item = await guard.admit("local", IP)
    before = await client.hgetall(store._key(WORK_KEY))
    assert await store.eval(ADMIT_LUA, *captured) == [0, 0]
    assert await client.hgetall(store._key(WORK_KEY)) == before
    assert await client.ttl(store._key(item.keys[2])) == -1
    await guard.release_work(item, BoundedWorkScope())
    assert not guard._owners
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1
    assert await client.hget(store._key(item.keys[2]), "state") == "released"
    assert 0 < await client.ttl(store._key(item.keys[2])) <= 600
    assert await store.eval(ADMIT_LUA, *captured) == [2, 0]
    await guard.complete(item, IP)
    refund = await client.hgetall(store._key(item.keys[0]))
    await guard.complete(item, IP)
    assert await client.hgetall(store._key(item.keys[0])) == refund
    assert await client.hgetall(store._key(WORK_KEY)) == before


@pytest.mark.parametrize("script_name", ["CANCEL_LUA", "RELEASE_LUA"])
async def test_r8_auth_wrong_owner_cannot_release_or_cancel(
    admission: tuple[Any, ...], script_name: str,
) -> None:
    from app.core.auth import admission as module

    guard, _, client, store, _, _ = admission
    item = await guard.admit("local", IP)
    before = await client.hgetall(store._key(ACTIVE_KEY))
    for token, binding in (("incorrect", item.binding), (item.token, "incorrect")):
        assert await store.eval(
            getattr(module, script_name), 2, ACTIVE_KEY, item.keys[2],
            item.request_id, token, binding,
        ) == -1
        assert await client.hgetall(store._key(ACTIVE_KEY)) == before
    await guard.release_work(item, BoundedWorkScope())


async def test_r8_auth_exhausted_recovery_stays_owned_and_capacity_is_bounded(
    admission: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.auth import admission as module

    guard, _, client, store, _, _ = admission
    monkeypatch.setattr(module, "RECOVERY_ATTEMPTS", 2)
    monkeypatch.setattr(module, "MAX_OWNERS", 1)
    item = await guard.admit("local", IP)
    calls = 0

    class Down:
        async def eval(self, script: str, *args: Any) -> Any:
            nonlocal calls
            calls += 1
            raise SessionStateUnavailable("synthetic unreachable Redis")

    guard.store = Down()
    work = BoundedWorkScope()
    with pytest.raises(SessionStateUnavailable, match="incomplete"):
        await guard.release_work(item, work)
    assert calls == 2 and len(guard._owners) == 1
    with pytest.raises(SessionStateUnavailable, match="capacity"):
        await guard.admit("local", IP)
    assert calls == 2
    guard.store = store
    await module.drain_login_admissions()
    assert not guard._owners
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1


async def test_r8_auth_cancelled_shutdown_waiter_does_not_cancel_owned_release(
    admission: tuple[Any, ...],
) -> None:
    from app.core.auth.admission import drain_login_admissions

    guard, _, client, store, _, _ = admission
    item = await guard.admit("local", IP)
    future = asyncio.get_running_loop().create_future()
    work = BoundedWorkScope()
    work.track(future)
    await guard.release_work(item, work)
    waiter = asyncio.create_task(drain_login_admissions())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not future.cancelled()
    assert await client.hlen(store._key(ACTIVE_KEY)) == 2
    future.set_result(None)
    assert await drain_login_admissions()
    assert await drain_login_admissions()
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1
    assert not guard._finishing and not guard._owners


async def test_r8_auth_shutdown_budget_reports_unknown_and_stops_redis_tasks(
    admission: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.auth import admission as module

    guard, _, client, store, _, _ = admission
    item = await guard.admit("local", IP)
    future = asyncio.get_running_loop().create_future()
    work = BoundedWorkScope()
    work.track(future)
    calls = 0

    class Observe:
        async def eval(self, script: str, *args: Any) -> Any:
            nonlocal calls
            assert not guard._closed
            calls += 1
            return await store.eval(script, *args)

    guard.store = Observe()
    monkeypatch.setattr(module, "DRAIN_TIMEOUT_S", 0.02)
    await guard.release_work(item, work)
    assert not await module.drain_login_admissions()
    assert guard._closed and not guard._finishing and len(guard._owners) == 1
    assert not future.cancelled()
    future.set_result(None)
    await asyncio.sleep(0)
    assert calls == 0
    assert await client.hlen(store._key(ACTIVE_KEY)) == 2
    assert await client.ttl(store._key(item.keys[2])) == -1
    with pytest.raises(SessionStateUnavailable):
        await guard.admit("local", IP)


@pytest.mark.parametrize("age_s", [121, 601])
async def test_r8_auth_known_owner_proof_survives_old_deadline_and_request_ttl(
    admission: tuple[Any, ...], age_s: int,
) -> None:
    guard, _, client, store, _, _ = admission
    item = await guard.admit("local", IP)
    seconds, micro = await client.time()
    deadline = (seconds - age_s + 120) * 1000 + micro // 1000
    value = f"{item.source}:local:{deadline}"
    # 只推进该合成请求的原发令时间；真实 Lua 仍以 Redis TIME 检查过期。
    await client.hset(store._key(ACTIVE_KEY), item.request_id, value)
    await client.hset(store._key(item.keys[2]), "active_value", value)
    assert await client.ttl(store._key(item.keys[2])) == -1
    with pytest.raises(SessionStateUnavailable):
        await guard.admit("ad", IP)
    await guard.release_work(item, BoundedWorkScope())
    if guard._finishing:
        await asyncio.gather(*guard._finishing)
    assert await client.hlen(store._key(ACTIVE_KEY)) == 1
