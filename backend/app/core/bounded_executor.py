"""同步 CPU/文件/DNS 工作的有界线程执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore, Lock
from typing import TypeVar

T = TypeVar("T")


class ExecutorBackpressure(RuntimeError):
    """执行器工作槽与排队预算均已耗尽。"""


class BoundedExecutor:
    """用线程信号量限制运行中与已排队工作，防止请求无界堆积。"""

    def __init__(self, *, max_workers: int = 4, max_pending: int = 8) -> None:
        if max_workers < 1 or max_pending < max_workers:
            raise ValueError("executor bounds are invalid")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="sms-bounded",
        )
        self._slots = BoundedSemaphore(max_pending)
        self._closed = False
        self._lock = Lock()

    async def run(
        self,
        function: Callable[..., T],
        /,
        *args: object,
        timeout_s: float,
        **kwargs: object,
    ) -> T:
        if timeout_s <= 0:
            raise ValueError("executor timeout must be positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("bounded executor is closed")
            acquired = self._slots.acquire(blocking=False)
        if not acquired:
            raise ExecutorBackpressure("同步工作队列已满，请稍后重试")

        def invoke() -> T:
            try:
                return function(*args, **kwargs)
            finally:
                self._slots.release()

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, invoke)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout_s)
        except TimeoutError:
            raise TimeoutError("同步工作超过允许时限") from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)


_BOUNDED_EXECUTOR: BoundedExecutor | None = None
_GLOBAL_LOCK = Lock()


def _current_executor() -> BoundedExecutor:
    global _BOUNDED_EXECUTOR
    with _GLOBAL_LOCK:
        if _BOUNDED_EXECUTOR is None:
            _BOUNDED_EXECUTOR = BoundedExecutor()
        return _BOUNDED_EXECUTOR


async def run_bounded[T](
    function: Callable[..., T],
    /,
    *args: object,
    timeout_s: float,
    **kwargs: object,
) -> T:
    return await _current_executor().run(
        partial(function, *args, **kwargs),
        timeout_s=timeout_s,
    )


def close_bounded_executor() -> None:
    global _BOUNDED_EXECUTOR
    with _GLOBAL_LOCK:
        executor = _BOUNDED_EXECUTOR
        _BOUNDED_EXECUTOR = None
    if executor is not None:
        executor.close()
