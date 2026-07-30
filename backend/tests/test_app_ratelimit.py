from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.app_ratelimit import (
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
)


class FakeRedis:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> int:
        self.calls.append(args)
        return self.allowed


@pytest.mark.asyncio
async def test_application_sliding_window_uses_app_key_and_atomic_lua() -> None:
    redis = FakeRedis(1)
    limiter = ApplicationRateLimiter(
        redis,
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        nonce=lambda: "request-1",
    )
    await limiter.check(app_id=7, limit_per_minute=60)
    call = redis.calls[0]
    assert call[1:3] == (1, "ratelimit:app:7")
    assert call[-2:] == ("60", "request-1")


@pytest.mark.asyncio
async def test_application_sliding_window_rejects_at_limit() -> None:
    with pytest.raises(ApplicationRateLimitExceeded):
        await ApplicationRateLimiter(FakeRedis(0)).check(app_id=7, limit_per_minute=1)
