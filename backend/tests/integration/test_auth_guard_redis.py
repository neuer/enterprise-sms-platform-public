from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.service import AccountLocked, LoginGuard, RateLimited, RedisKeyValue
from app.services.runtime_policy import RuntimePolicy

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated Redis 7",
)


class PrefixedRedisKeyValue:
    """让真实 Redis 合同测试只触碰本次随机命名空间。"""

    def __init__(self, delegate: RedisKeyValue, prefix: str) -> None:
        self.delegate = delegate
        self.prefix = prefix
        self.keys: set[str] = set()

    def _key(self, value: object) -> str:
        key = f"{self.prefix}:{value}"
        self.keys.add(key)
        return key

    async def get(self, key: str) -> Any:
        return await self.delegate.get(self._key(key))

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        await self.delegate.set(self._key(key), value, ex=ex)

    async def delete(self, key: str) -> None:
        await self.delegate.delete(self._key(key))

    async def increment(self, key: str, *, window_s: int) -> int:
        return await self.delegate.increment(self._key(key), window_s=window_s)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        keys = tuple(self._key(item) for item in args[:numkeys])
        return await self.delegate.eval(script, numkeys, *keys, *args[numkeys:])


async def _policy() -> RuntimePolicy:
    return RuntimePolicy.from_mapping({})


@pytest.mark.asyncio
async def test_real_redis_lock_ban_ttl_and_concurrency_contract() -> None:
    url = os.environ["AUTH_GUARD_REDIS_URL"]
    first_client = Redis.from_url(url, decode_responses=True)
    second_client = Redis.from_url(url, decode_responses=True)
    prefix = f"auth-guard-test:{uuid4()}"
    first_store = PrefixedRedisKeyValue(RedisKeyValue(first_client), prefix)
    second_store = PrefixedRedisKeyValue(RedisKeyValue(second_client), prefix)
    first = LoginGuard(first_store, policy_loader=_policy)
    second = LoginGuard(second_store, policy_loader=_policy)
    try:
        await first_client.ping()
        outcomes: list[type[Exception]] = []
        for attempt in range(5):
            guard = first if attempt % 2 == 0 else second
            ip = f"10.0.0.{41 + attempt}"
            try:
                await guard.admit("local", "shared-user", ip)
                await guard.record_failure("shared-user", ip, "local")
            except AccountLocked as error:
                outcomes.append(type(error))
            else:
                outcomes.append(Exception)

        assert outcomes == [Exception] * 4 + [AccountLocked]
        lock_key = first_store._key("auth:lock:user:shared-user")
        lock_value = await first_client.get(lock_key)
        assert str(UUID(str(lock_value))) == lock_value
        await second.admit("local", "shared-user", "10.0.0.99")
        assert await first_client.get(lock_key) == lock_value
        await second.record_bound_success("shared-user")
        assert await first_client.get(lock_key) is None
        assert await first_client.get(first_store._key("auth:fail:user:shared-user")) is None

        ban_ip = "10.0.0.140"
        ban_outcomes: list[type[Exception]] = []
        for attempt in range(20):
            guard = first if attempt % 2 == 0 else second
            try:
                await guard.record_failure(f"ip-target-{attempt}", ban_ip, "local")
            except RateLimited as error:
                ban_outcomes.append(type(error))
            else:
                ban_outcomes.append(Exception)
        assert ban_outcomes == [Exception] * 19 + [RateLimited]
        ban_key = first_store._key(f"auth:ban:ip:{ban_ip}")
        ban_value = await first_client.get(ban_key)
        assert str(UUID(str(ban_value))) == ban_value
        assert int(await first_client.get(first_store._key(f"auth:fail:ip:{ban_ip}"))) == 20

        ban_ttl_before = await first_client.pttl(ban_key)
        with pytest.raises(RateLimited):
            await second.admit("local", "shared-user", ban_ip)
        ban_ttl_after = await first_client.pttl(ban_key)
        assert 0 < ban_ttl_after <= ban_ttl_before

        concurrent_ip = "10.0.0.42"

        async def concurrency_policy() -> RuntimePolicy:
            return RuntimePolicy.from_mapping({"login_ip_fail_limit": "1000"})

        first_concurrent = LoginGuard(first_store, policy_loader=concurrency_policy)
        second_concurrent = LoginGuard(second_store, policy_loader=concurrency_policy)
        results = await asyncio.gather(
            *(
                (first_concurrent if index % 2 == 0 else second_concurrent).record_failure(
                    "concurrent-user",
                    concurrent_ip,
                    "ad",
                )
                for index in range(50)
            ),
            return_exceptions=True,
        )
        assert not any(isinstance(item, SessionStateUnavailable) for item in results)
        assert int(await first_client.get(first_store._key("auth:fail:user:concurrent-user"))) == 50
        concurrent_lock = await first_client.get(first_store._key("auth:lock:user:concurrent-user"))
        assert str(UUID(str(concurrent_lock))) == concurrent_lock
    finally:
        keys = first_store.keys | second_store.keys
        if keys:
            await first_client.delete(*keys)
        await first_client.aclose()
        await second_client.aclose()
