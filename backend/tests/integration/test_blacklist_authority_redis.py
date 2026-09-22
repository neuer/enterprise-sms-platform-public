from __future__ import annotations

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.blacklist import BLACKLIST_MATCH_LUA

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7"
)


@pytest.mark.asyncio
async def test_atomic_blacklist_read_requires_loaded_snapshot_and_supports_large_batches() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    key = "blacklist:test:" + uuid4().hex
    loaded = key + ":loaded"
    candidates = [format(index, "064x") for index in range(10000)]
    try:
        await redis.sadd(key, candidates[0])
        assert await redis.eval(BLACKLIST_MATCH_LUA, 2, loaded, key, *candidates) is None
        await redis.set(loaded, "1")
        flags = await redis.eval(BLACKLIST_MATCH_LUA, 2, loaded, key, *candidates)
        assert flags == [1] + [0] * 9999
        # 失效后原 SET 即使存在也不可读；重建完成后只能返回新快照。
        await redis.delete(loaded)
        assert await redis.eval(BLACKLIST_MATCH_LUA, 2, loaded, key, *candidates) is None
        async with redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.sadd(key, candidates[-1])
            pipe.set(loaded, "1")
            await pipe.execute()
        flags = await redis.eval(BLACKLIST_MATCH_LUA, 2, loaded, key, *candidates)
        assert flags == [0] * 9999 + [1]
    finally:
        await redis.delete(key, loaded)
        await redis.aclose()
