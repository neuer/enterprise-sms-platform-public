"""真实 PG/Redis 验证重建 Lua、跨上海午夜及取消后的会话锁释放。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.services import usage_ledger as module
from app.services.usage_ledger import ProjectionRow, UsageLedgerService, UsageProjectionUnavailable

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)


class NamespacedRedis:
    """仅给测试键加隔离前缀；Lua 原文、命令及返回值全部交由真实 Redis。"""

    def __init__(self, client: Redis, prefix: str) -> None:
        self.client = client
        self.prefix = prefix
        self.scripts: list[str] = []
        self.after_apply: Callable[[], Awaitable[None]] | None = None

    def key(self, key: str) -> str:
        return self.prefix + key

    async def get(self, key: str) -> Any:
        return await self.client.get(self.key(key))

    async def set(self, key: str, value: Any, **kwargs: Any) -> Any:
        return await self.client.set(self.key(key), value, **kwargs)

    async def mget(self, keys: Sequence[str]) -> list[Any]:
        return await self.client.mget([self.key(key) for key in keys])

    async def eval(self, script: str, numkeys: int, *arguments: Any) -> Any:
        self.scripts.append(script)
        result = await self.client.eval(
            script,
            numkeys,
            *(self.key(str(key)) for key in arguments[:numkeys]),
            *arguments[numkeys:],
        )
        if script == module.APPLY_PROJECTIONS_LUA and self.after_apply is not None:
            await self.after_apply()
        return result


@dataclass
class ProjectionRuntime:
    owner: AsyncEngine
    engine: AsyncEngine
    application_name: str
    redis: NamespacedRedis
    service: UsageLedgerService
    clock: list[datetime]


@pytest.fixture
async def projection_runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ProjectionRuntime]:
    """复用官方一次性服务，只清理随机 schema 和本例 Redis 前缀。"""

    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    redis_url = os.environ["AUTH_GUARD_REDIS_URL"]
    assert database_url.host in {"127.0.0.1", "localhost"}
    assert urlsplit(redis_url).hostname in {"127.0.0.1", "localhost"}
    namespace = f"usage_runtime_{uuid4().hex}"
    owner = create_async_engine(database_url)
    async with owner.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
        await connection.execute(text(
            f'CREATE TABLE "{namespace}".usage_projection '
            '(LIKE public.usage_projection INCLUDING ALL)'
        ))
    engine = create_async_engine(database_url, connect_args={"server_settings": {
        "search_path": f"{namespace},public", "application_name": namespace,
    }})
    client = Redis.from_url(redis_url, decode_responses=True)
    redis = NamespacedRedis(client, f"test:{namespace}:")
    # 明确推进服务时钟跨午夜；绝对过期时间仍晚于真实 Redis 服务器当前时间。
    server_now = datetime.fromtimestamp((await client.time())[0], UTC)
    midnight = module.shanghai_day(server_now)[2] + timedelta(days=1)
    clock = [midnight - timedelta(seconds=1)]
    service = UsageLedgerService(
        redis, cast(Any, SimpleNamespace(database_url=database_url)), clock=lambda: clock[0],
    )
    service._engine = lambda: engine  # type: ignore[method-assign]
    monkeypatch.setattr(module, "PROJECTION_BATCH_ROWS", 1)
    try:
        yield ProjectionRuntime(owner, engine, namespace, redis, service, clock)
    finally:
        await engine.dispose()
        keys = [key async for key in client.scan_iter(match=f"{redis.prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
        async with owner.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        await owner.dispose()


async def seed_projections(runtime: ProjectionRuntime) -> list[ProjectionRow]:
    """两条合成事实强制跨页，次日事实供跨午夜完成边界验证。"""

    _key, day, boundary = module.shanghai_day(runtime.clock[0])
    rows = [
        ProjectionRow("quota:app:1:day", "quota", day, 3, 101, boundary),
        ProjectionRow(
            "quota:app:2:day", "quota", day + timedelta(days=1),
            7, 102, boundary + timedelta(days=1),
        ),
    ]
    async with runtime.engine.begin() as connection:
        for row in rows:
            await connection.execute(text("""
                INSERT INTO usage_projection(
                  dimension_key,kind,usage_date,window_key,value,version,expires_at
                ) VALUES(:key,:kind,:day,'day',:value,:version,:expires)
            """), {
                "key": row.dimension_key, "kind": row.kind, "day": row.usage_date,
                "value": row.value, "version": row.version, "expires": row.expires_at,
            })
    return rows


@pytest.mark.asyncio
async def test_real_projection_lua_owner_ttl_and_absolute_version_fencing(
    projection_runtime: ProjectionRuntime,
) -> None:
    runtime = projection_runtime
    service, redis = runtime.service, runtime.redis
    day_key, day, boundary = module.shanghai_day(runtime.clock[0])
    ready = module._ready_key(day_key)
    await redis.set(ready, "stale")
    async with runtime.engine.connect() as connection:
        token = await service._claim_rebuild_lock(connection, day_key)
        try:
            assert await redis.get(ready) is None
            assert await redis.get(module.PROJECTION_REBUILD_KEY) == token
            await redis.client.expire(redis.key(module.PROJECTION_REBUILD_KEY), 1)
            await service._renew_rebuild(token)
            assert await redis.client.ttl(redis.key(module.PROJECTION_REBUILD_KEY)) > 250
            with pytest.raises(UsageProjectionUnavailable, match="owner lost"):
                await service._renew_rebuild("stale-owner")
            for rejected in ("", "stale-owner"):
                with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
                    await service._publish_ready({day: boundary}, token=rejected)
            assert await redis.get(ready) is None
            await service._publish_ready({day: boundary}, token=token)
            assert await redis.get(ready) == "1"
            assert await redis.get(module.PROJECTION_REBUILD_KEY) is None
            assert await redis.client.pexpiretime(redis.key(ready)) == int(
                boundary.timestamp() * 1000
            )
        finally:
            await service._release_rebuild_lock(connection)

    row = ProjectionRow("quota:app:1:version", "quota", day, 8, 3, boundary)
    assert await service._apply_rows([row]) == 1
    assert await service._apply_rows([replace(row, value=99, version=2)]) == 0
    assert await redis.get(row.dimension_key) == "8"
    assert await redis.get(module._version_key(row.dimension_key)) == "3"
    await service.ensure_ready()


@pytest.mark.asyncio
async def test_real_rebuild_clock_crosses_midnight_without_early_ready(
    projection_runtime: ProjectionRuntime,
) -> None:
    runtime = projection_runtime
    rows = await seed_projections(runtime)
    old_key, _day, _boundary = module.shanghai_day(runtime.clock[0])
    tomorrow_key = rows[1].usage_date.strftime("%Y%m%d")
    for key in (old_key, tomorrow_key):
        await runtime.redis.set(module._ready_key(key), "stale")
    calls = 0

    async def cross_midnight() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert await runtime.redis.get(module._ready_key(old_key)) is None
            runtime.clock[0] += timedelta(seconds=2)
            assert module.shanghai_day(runtime.clock[0])[0] == tomorrow_key
            # 即使下一天已有旧 marker，实际 MGET 仍以全局重建屏障失败关闭。
            with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
                await runtime.service.ensure_ready()

    runtime.redis.after_apply = cross_midnight
    assert await runtime.service.rebuild() == 2
    assert calls == 2
    assert runtime.redis.scripts.count(module.BEGIN_PROJECTION_REBUILD_LUA) == 1
    assert runtime.redis.scripts.count(module.RENEW_PROJECTION_REBUILD_LUA) == 2
    assert runtime.redis.scripts.count(module.PUBLISH_PROJECTION_READY_LUA) == 1
    assert await runtime.redis.get(module.PROJECTION_REBUILD_KEY) is None
    for row in rows:
        assert await runtime.redis.get(row.dimension_key) == str(row.value)
        assert await runtime.redis.get(module._ready_key(row.usage_date.strftime("%Y%m%d"))) == "1"
    await runtime.service.ensure_ready()


@pytest.mark.asyncio
async def test_real_rebuild_midpage_failure_stays_closed_and_retry_is_absolute(
    projection_runtime: ProjectionRuntime,
) -> None:
    runtime = projection_runtime
    rows = await seed_projections(runtime)

    async def fail_after_write() -> None:
        raise ConnectionError("synthetic lost response after real Redis write")

    runtime.redis.after_apply = fail_after_write
    with pytest.raises(UsageProjectionUnavailable, match="write unavailable"):
        await runtime.service.rebuild()
    assert await runtime.redis.get(rows[0].dimension_key) == str(rows[0].value)
    assert await runtime.redis.get(rows[1].dimension_key) is None
    assert await runtime.redis.get(module.PROJECTION_REBUILD_KEY) is not None
    for row in rows:
        assert await runtime.redis.get(module._ready_key(row.usage_date.strftime("%Y%m%d"))) is None
    with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
        await runtime.service.ensure_ready()
    runtime.redis.after_apply = None
    assert await runtime.service.rebuild() == 2
    for row in rows:
        assert await runtime.redis.get(row.dimension_key) == str(row.value)
    assert await runtime.redis.get(module.PROJECTION_REBUILD_KEY) is None


@pytest.mark.asyncio
async def test_cancelled_real_rebuild_releases_live_pooled_session_lock(
    projection_runtime: ProjectionRuntime,
) -> None:
    runtime = projection_runtime
    rows = await seed_projections(runtime)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def wait_after_write() -> None:
        entered.set()
        await blocked.wait()

    runtime.redis.after_apply = wait_after_write
    async with runtime.owner.connect() as competitor:
        task = asyncio.create_task(runtime.service.rebuild())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            owner_pid = await competitor.scalar(text("""
                SELECT pid FROM pg_stat_activity WHERE application_name=:name
                  AND state='idle in transaction'
            """), {"name": runtime.application_name})
            assert owner_pid is not None
            day_key = module.shanghai_day(runtime.clock[0])[0]
            with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
                await runtime.service._claim_rebuild_lock(competitor, day_key)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # 不靠销毁连接掩盖泄漏：原池化 backend 仍存活，但已无 session advisory lock。
            assert await competitor.scalar(text(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"
            ), {"pid": owner_pid})
            assert await competitor.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory'"
            ), {"pid": owner_pid}) == 0
            assert await runtime.redis.get(module._ready_key(day_key)) is None
            assert await runtime.redis.get(rows[0].dimension_key) == str(rows[0].value)
            with pytest.raises(UsageProjectionUnavailable, match="rebuild in progress"):
                await runtime.service.ensure_ready()
            await runtime.service._claim_rebuild_lock(competitor, day_key)
            await runtime.service._release_rebuild_lock(competitor)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
