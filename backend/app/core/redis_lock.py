"""带心跳续租的 Redis 锁包装，防止长任务处理期间租约过期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from redis.exceptions import LockError

LOGGER = logging.getLogger(__name__)


class LeaseUncertain(RuntimeError):
    """续租不可确认；在安全点停止新外呼，不能取消已消费响应的捕获。"""


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
        self._uncertain = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._acquired = False

    @property
    def lease_uncertain(self) -> bool:
        return self._uncertain.is_set()

    async def wait_uncertain(self) -> None:
        await self._uncertain.wait()

    def ensure_lease(self) -> None:
        """仅在外呼前或 raw 已可靠捕获后检查，禁止中断消费中的响应。"""
        if self.lease_uncertain or not self._acquired:
            raise LeaseUncertain("poll lease unavailable")

    def _mark_uncertain(self, reason: str) -> None:
        if not self._uncertain.is_set():
            self._uncertain.set()
            LOGGER.warning("redis lock renewal uncertain", extra={"reason": reason})

    async def acquire(self) -> bool:
        async with self._lifecycle:
            if self._acquired or not await self._lock.acquire(blocking=False):
                return False
            self._acquired = True
            self._uncertain.clear()
            self._task = asyncio.create_task(self._heartbeat())
            return True

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._beat_s)
            try:
                # 重置为固定租期，禁止每次累加 TTL。
                renewed = await self._lock.extend(self._ttl_s, replace_ttl=True)
                if renewed is False:
                    self._mark_uncertain("extend_rejected")
                    return
            except asyncio.CancelledError:
                raise
            except LockError:
                self._mark_uncertain("owner_lost")
                return
            except Exception:
                self._mark_uncertain("renewal_unavailable")
                return

    async def release(self) -> bool:
        """只释放本次持有的 owner token；重复释放不访问底层锁。"""
        async with self._lifecycle:
            if not self._acquired:
                return False
            if self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            self._acquired = False
            try:
                await self._lock.release()
            except LockError:
                self._mark_uncertain("owner_lost")
                return False
            except Exception:
                self._mark_uncertain("release_unavailable")
                return False
            return not self.lease_uncertain
