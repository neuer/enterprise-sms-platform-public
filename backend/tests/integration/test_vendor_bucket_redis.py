"""真实 Redis 执行原始 TokenBucket Lua，验证小规模并发原子性。"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.ratelimit import TokenBucket

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7"
)


@pytest.mark.asyncio
async def test_real_redis_vendor_bucket_concurrency_and_refund() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    key = f"ratelimit:vendor:test-{uuid4().hex}"

    # 仅替换 Redis TIME 依赖；被执行的桶算法直接来自 TokenBucket.eval 参数。
    class ServerClock:
        async def eval(self, script: str, n: int, *args: Any) -> Any:
            shim = """
                local original_call = redis.call
                local redis = {call = function(cmd, ...)
                  if cmd == 'TIME' then return {'1000','0'} end
                  return original_call(cmd, ...)
                end}
            """
            return await redis.eval(shim + script, n, *args)

    try:
        bucket = TokenBucket(ServerClock(), key=key)
        results = await asyncio.gather(
            *(
                bucket.acquire(lane="realtime", vendor_qps=5, reserved_realtime_qps=2)
                for _ in range(20)
            )
        )
        assert sum(item is not None for item in results) == 5
        assert await redis.hgetall(key) == {"tokens": "0", "last_ms": "1000000"}
        await bucket.refund(vendor_qps=5, lease_epoch=1000000)
        assert (
            await bucket.acquire(lane="realtime", vendor_qps=5, reserved_realtime_qps=2) == 1000000
        )
        assert await bucket.acquire(lane="realtime", vendor_qps=5, reserved_realtime_qps=2) is None
    finally:
        await redis.delete(key)
        await redis.aclose()
