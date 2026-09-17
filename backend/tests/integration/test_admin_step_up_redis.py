"""隔离 Redis 7 执行实际单次消费 Lua 与有效期边界。"""

from __future__ import annotations

import asyncio
import os

import pytest
from redis.asyncio import Redis

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.errors import ApiError
from app.services.admin_step_up import _key
from tests.test_admin_step_up import CLAIMS, INTENT, setup

pytestmark = pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="isolated Redis 7")


@pytest.mark.asyncio
async def test_actual_redis_single_consumption_and_expiry() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    service, _, _, _ = setup()
    service.store = redis
    keys: list[str] = []
    try:
        token = await service.issue(
            claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
        )
        keys.append(_key(token))
        assert 0 < await redis.ttl(keys[-1]) <= 300
        results = await asyncio.gather(
            *[
                service.consume(
                    token, claims=CLAIMS, access_token="access", ip="synthetic", intent=INTENT
                )
                for _ in range(20)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(item, AdminAuthorization) for item in results) == 1
        assert sum(isinstance(item, ApiError) for item in results) == 19
        token = await service.issue(
            claims=CLAIMS, password="synthetic-valid", ip="synthetic", intent=INTENT
        )
        keys.append(_key(token))
        await redis.pexpire(keys[-1], 1)
        await asyncio.sleep(0.02)
        with pytest.raises(ApiError):
            await service.consume(
                token, claims=CLAIMS, access_token="access", ip="synthetic", intent=INTENT
            )
    finally:
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
