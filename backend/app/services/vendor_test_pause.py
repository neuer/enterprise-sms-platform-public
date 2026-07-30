"""真实联调独立暂停键的共享写入原语。"""

from __future__ import annotations

from typing import Any

_AGENT_STALE_PAUSE_SCRIPT = (
    "redis.call('set',KEYS[1],ARGV[1]); "
    "redis.call('set',KEYS[2],ARGV[1]); return 1"
)


async def pause_vendor_test_agent_stale(redis: Any) -> None:
    """控制状态损坏或过期时设置需人工恢复的独立 critical pause。"""

    result = await redis.eval(
        _AGENT_STALE_PAUSE_SCRIPT,
        2,
        "queue:paused:vendor-test-agent-stale:realtime",
        "queue:paused:vendor-test-agent-stale:bulk",
        "vendor-test-agent-stale",
    )
    if result != 1:
        raise RuntimeError("agent stale pause was not persisted")
