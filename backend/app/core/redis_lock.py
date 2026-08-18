"""带心跳续租的 Redis 锁包装，防止长任务处理期间租约过期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from redis.exceptions import LockError

LOGGER = logging.getLogger(__name__)


class HeartbeatLock:
    """acquire 后按固定节拍把剩余 TTL 重置为完整租期。

    用于 poll 等可能超过单次租期的长任务：锁只是并发抑制手段，事实
    幂等性由 DB 唯一约束兜底，所以 release 容忍租约已被他人接管——
    返回 False 由调用方记录告警，不把已完成的工作标记为失败。
    """

    def __init__(self, lock: Any, *, ttl_s: int, beat_s: int) -> None:
        if not 1 <= beat_s < ttl_s:
            raise ValueError("heartbeat interval must be shorter than lock ttl")
        self._lock = lock
        self._ttl_s = ttl_s
        self._beat_s = beat_s
        self._task: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        if not await self._lock.acquire(blocking=False):
            return False

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(self._beat_s)
                try:
                    # replace_ttl=True 重置为固定租期；缺省累加语义会让
                    # 长持有者的 TTL 无界增长。
                    await self._lock.extend(self._ttl_s, replace_ttl=True)
                except Exception:
                    return

        self._task = asyncio.create_task(_heartbeat())
        return True

    async def release(self) -> bool:
        """返回 False 表示租约已失（锁已过期或被他人接管）。"""

        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await self._lock.release()
        except LockError as exc:
            LOGGER.warning(
                "redis lock lease lost before release",
                extra={"error_type": type(exc).__name__},
            )
            return False
        return True
