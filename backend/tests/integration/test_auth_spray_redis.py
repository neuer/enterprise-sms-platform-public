"""仅隔离 Redis：跨实例失败信号、容量、隐私和 TTL。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.auth.admission_policy import AdmissionLimits
from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.service import RedisKeyValue
from app.core.auth.spray import PasswordSprayGuard
from tests.integration.test_auth_guard_redis import PrefixedRedisKeyValue

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis"
)


@pytest.mark.asyncio
async def test_two_instances_aggregate_failure_sources_and_expire() -> None:
    clients = [
        Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True) for _ in range(2)
    ]
    prefix = "spray-test:" + uuid4().hex
    stores = [PrefixedRedisKeyValue(RedisKeyValue(c), prefix) for c in clients]
    loader = AsyncMock(return_value=SimpleNamespace(limits=AdmissionLimits()))
    guards = [PasswordSprayGuard(s, loader, key=b"synthetic-key") for s in stores]
    try:
        results = await asyncio.gather(
            *(guards[i % 2].record_failure("victim", f"192.0.2.{i + 1}") for i in range(40))
        )
        assert max(results) == 0.5
        keys = set.union(*(s.keys for s in stores))
        assert len(keys) <= 41
        key = next(k for k in keys if ":account:" in k)
        state = await clients[0].hgetall(key)
        assert state["count"] == "40" and len(state) <= 33
        assert "victim" not in str(state) and "192.0.2" not in str(state)
        assert 0 < await clients[0].ttl(key) <= 900
        # 同来源跨名称的枚举/喷洒信号独立于单账号多来源。
        enumeration = await asyncio.gather(
            *(
                guards[i % 2].record_failure(f"synthetic-name-{i}", "198.51.100.9")
                for i in range(40)
            )
        )
        assert max(enumeration) == 0.5
        # TTL=0 表示仍存活的最后一秒，不得误当持久或损坏状态。
        await clients[0].pexpire(key, 900)
        assert await guards[0].record_failure("victim", "192.0.2.90") == 0.5
        await clients[0].hset(key, "count", "corrupt")
        with pytest.raises(SessionStateUnavailable):
            await guards[1].record_failure("victim", "192.0.2.90")
        await clients[0].pexpire(key, 1)
        await asyncio.sleep(0.02)
        assert await guards[0].record_failure("victim", "192.0.2.90") == 0
    finally:
        keys = set.union(*(s.keys for s in stores))
        if keys:
            await clients[0].delete(*keys)
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_official_auth_acl_keeps_spray_and_legacy_failure_accounting() -> None:
    import re
    from pathlib import Path

    from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
    from app.core.auth.service import AuthService, LoginGuard

    admin = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    username = "spray-acl-" + uuid4().hex
    synthetic_password = "isolated-test-only-credential"

    def read_acl() -> str:
        path = Path(__file__).resolve().parents[3] / "deploy/redis-domain-entrypoint.sh"
        return path.read_text()

    source = await asyncio.to_thread(read_acl)
    section = source.split("  auth)\n", 1)[1].split("    ;;", 1)[0]
    rules = re.search(r"command_rules='([^']+)'", section).group(1).split()
    keys = re.search(r"key_rules='([^']+)'", section).group(1).split()
    restricted = None
    store = None
    try:
        await admin.execute_command(
            "ACL", "SETUSER", username, "on", ">" + synthetic_password, "-@all", *keys, *rules
        )
        restricted = Redis.from_url(
            os.environ["AUTH_GUARD_REDIS_URL"],
            username=username,
            password=synthetic_password,
            decode_responses=True,
        )
        store = PrefixedRedisKeyValue(RedisKeyValue(restricted), "auth:acl-test:" + uuid4().hex)
        loader = AsyncMock(return_value=SimpleNamespace(limits=AdmissionLimits()))
        spray = PasswordSprayGuard(store, loader, key=b"synthetic-key")
        guard = LoginGuard(store, spray=spray)
        provider = SimpleNamespace(authenticate=AsyncMock(side_effect=InvalidCredentials()))
        service = AuthService(provider, guard)
        with pytest.raises(InvalidCredentials):
            await service.authenticate("local", "synthetic-user", "synthetic-password", "192.0.2.1")
        assert await store.get("auth:fail:user:synthetic-user") == "1"
        assert await store.get("auth:fail:ip:192.0.2.1") == "1"
        for i in range(20):
            await spray.record_failure("synthetic-user", f"198.51.100.{i + 1}")
        provider.authenticate.side_effect = None
        provider.authenticate.return_value = AuthenticatedIdentity(
            "local", "synthetic-user", "synthetic-user", "Test", "", ()
        )
        assert (
            await service.authenticate("local", "synthetic-user", "valid", "192.0.2.2")
        ).login_name == "synthetic-user"
        await service.record_bound_success("synthetic-user")
        assert await store.get("auth:fail:user:synthetic-user") is None
    finally:
        if restricted is not None:
            await restricted.aclose()
        if store is not None and store.keys:
            await admin.delete(*store.keys)
        await admin.execute_command("ACL", "DELUSER", username)
        await admin.aclose()
