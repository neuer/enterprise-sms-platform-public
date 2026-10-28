"""带心跳续租的 Redis 锁包装，防止长任务处理期间租约过期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic

from redis.asyncio.lock import Lock
from redis.exceptions import LockError

LOGGER = logging.getLogger(__name__)
LEASE_FAILURE_REASONS = frozenset({
    "extend_rejected", "owner_lost", "renewal_unavailable",
    "release_unavailable", "deadline_elapsed",
})


class LeaseUncertain(RuntimeError):
    """续租不可确认；在安全点停止新外呼，不能取消已消费响应的捕获。"""


class HeartbeatLock:
    """acquire 后按固定节拍把剩余 TTL 重置为完整租期。

    用于 poll 等可能超过单次租期的长任务：锁只是并发抑制手段，事实
    幂等性由 DB 唯一约束兜底，所以 release 容忍租约已被他人接管——
    返回 False 由调用方记录告警，不把已完成的工作标记为失败。
    """

    def __init__(self, lock: Lock, *, ttl_s: int, beat_s: int) -> None:
        if not 1 <= beat_s < ttl_s:
            raise ValueError("heartbeat interval must be shorter than lock ttl")
        self._lock = lock
        self._ttl_s = ttl_s
        self._beat_s = beat_s
        self._task: asyncio.Task[None] | None = None
        self._uncertain = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._acquired = False
        self._deadline = 0.0
        self._reason: str | None = None
        self._stop_reported = False

    @property
    def lease_uncertain(self) -> bool:
        self._check_deadline()
        return self._uncertain.is_set()

    @property
    def uncertain_reason(self) -> str | None:
        return self._reason

    def _check_deadline(self) -> None:
        if self._acquired and monotonic() >= self._deadline:
            self._mark_uncertain("deadline_elapsed")

    async def wait_uncertain(self) -> None:
        await self._uncertain.wait()

    def ensure_lease(self) -> None:
        """仅在外呼前或 raw 已可靠捕获后检查，禁止中断消费中的响应。"""
        if self.lease_uncertain or not self._acquired:
            if not self._stop_reported:
                self._stop_reported = True
                LOGGER.warning("poll stopped at lease safety boundary")
            raise LeaseUncertain("poll lease unavailable")

    def _mark_uncertain(self, reason: str) -> None:
        if reason not in LEASE_FAILURE_REASONS:
            raise ValueError("unknown lease failure reason")
        if not self._uncertain.is_set():
            self._reason = reason
            self._uncertain.set()
            LOGGER.warning("redis lock renewal uncertain", extra={"reason": reason})

    async def acquire(self) -> bool:
        async with self._lifecycle:
            started = monotonic()
            if self._acquired or not await self._lock.acquire(blocking=False):
                return False
            after_uncertain = self._uncertain.is_set()
            self._acquired = True
            self._uncertain.clear()
            self._reason = None
            self._stop_reported = False
            self._deadline = started + self._ttl_s
            self._check_deadline()
            self._task = asyncio.create_task(self._heartbeat())
            LOGGER.info(
                "poll lease acquisition completed",
                extra={"after_uncertain": after_uncertain, "usable": not self.lease_uncertain},
            )
            return True

    async def _heartbeat(self) -> None:
        while not self.lease_uncertain:
            await asyncio.sleep(min(self._beat_s, max(0, self._deadline - monotonic())))
            if self.lease_uncertain:
                return
            started = monotonic()
            try:
                # 截止时间从请求发出前计；迟到成功不能抹掉已出现的租约空窗。
                async with asyncio.timeout(max(0, self._deadline - started)):
                    renewed = await self._lock.extend(self._ttl_s, replace_ttl=True)
                if self.lease_uncertain:
                    return
                if renewed is False:
                    self._mark_uncertain("extend_rejected")
                    return
                self._deadline = started + self._ttl_s
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._mark_uncertain("deadline_elapsed")
                return
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
            self._check_deadline()
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
