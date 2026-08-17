"""真实联调控制状态不可用时的 fail-closed 与 agent-stale latch。"""

from __future__ import annotations

import logging
from typing import Any

from redis.asyncio import Redis

from app.core.errors import ApiError
from app.services.vendor_control_state import VendorControlStateUnavailable
from app.services.vendor_test_pause import pause_vendor_test_agent_stale
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)


async def pause_vendor_test_agent_stale_keys(settings: Any) -> None:
    """把 agent-stale critical pause 写入独立 Redis 控制面。"""

    redis: Any = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        await pause_vendor_test_agent_stale(redis)
    finally:
        await redis.aclose()


async def raise_vendor_control_unavailable(
    error: VendorControlStateUnavailable,
    *,
    message: str = "真实联调控制代理状态不可用",
) -> None:
    """损坏状态写入 critical pause；写入失败保持 503 且不得继续。"""

    if error.requires_critical_pause:
        try:
            await pause_vendor_test_agent_stale_keys(get_settings())
        except Exception as pause_error:
            LOGGER.error(
                "vendor test agent stale pause unavailable",
                extra={"error_type": type(pause_error).__name__},
            )
            raise ApiError(
                503,
                "CONTROL_AGENT_PAUSE_UNAVAILABLE",
                "真实联调安全暂停未确认，发送保持关闭",
                None,
            ) from None
    raise ApiError(
        503,
        "CONTROL_AGENT_UNAVAILABLE",
        message,
        None,
    ) from None
