"""同步 CPU/文件/DNS 工作的有界线程执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from threading import BoundedSemaphore, Lock
from typing import Any, Literal, TypeVar

T = TypeVar("T")
ExecutorPool = Literal[
    "default",
    "ldap",
    "archive",
    "smtp",
    "auth_login_hash",
    "auth_hash",
]

_POOL_BOUNDS: dict[ExecutorPool, tuple[int, int]] = {
    "default": (4, 8),
    "ldap": (4, 8),
    "archive": (4, 8),
    "smtp": (4, 8),
    # Argon2 每个任务使用 64 MiB。交互式登录保留独立容量，避免已认证的
    # 改密、step-up 或管理员密码维护挤占登录验证池。
    "auth_login_hash": (1, 2),
    "auth_hash": (1, 2),
}


class ExecutorBackpressure(RuntimeError):
    """执行器工作槽与排队预算均已耗尽。"""


class BoundedWorkScope:
    """记录单个 bulk 工作仍在执行的线程 Future，取消协程不代表线程结束。"""

    def __init__(self) -> None:
        self._pending: set[asyncio.Future[Any]] = set()
        self._lock = Lock()

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def track(self, future: asyncio.Future[Any]) -> None:
        with self._lock:
            self._pending.add(future)

        def completed(item: asyncio.Future[Any]) -> None:
            with self._lock:
                self._pending.discard(item)
            if not item.cancelled():
                item.exception()  # 原调用已超时也必须观察后台线程的异常。

        future.add_done_callback(completed)

    async def wait_finished(self) -> None:
        """在原协程退出后等待线程事实收尾；不向线程 Future 传播取消。"""

        while True:
            with self._lock:
                pending = tuple(self._pending)
            if not pending:
                return
            await asyncio.gather(
                *(asyncio.shield(item) for item in pending), return_exceptions=True
            )


_WORK_SCOPE: ContextVar[BoundedWorkScope | None] = ContextVar("bounded_work_scope", default=None)


@contextmanager
def bounded_work_scope() -> Iterator[BoundedWorkScope]:
    """仅 bulk 准入显式启用；共享对象随 ContextVar 传入 worker 常驻 loop。"""

    scope = BoundedWorkScope()
    token = _WORK_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _WORK_SCOPE.reset(token)


class BoundedExecutor:
    """用线程信号量限制运行中与已排队工作，防止请求无界堆积。"""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_pending: int = 8,
        thread_name_prefix: str = "sms-bounded",
    ) -> None:
        if max_workers < 1 or max_pending < max_workers:
            raise ValueError("executor bounds are invalid")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
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
        scope = _WORK_SCOPE.get()
        if scope is not None:
            scope.track(future)
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


_BOUNDED_EXECUTORS: dict[ExecutorPool, BoundedExecutor] = {}
_GLOBAL_LOCK = Lock()


def _current_executor(pool: ExecutorPool) -> BoundedExecutor:
    with _GLOBAL_LOCK:
        executor = _BOUNDED_EXECUTORS.get(pool)
        if executor is None:
            max_workers, max_pending = _POOL_BOUNDS[pool]
            executor = BoundedExecutor(
                max_workers=max_workers,
                max_pending=max_pending,
                thread_name_prefix=f"sms-{pool}",
            )
            _BOUNDED_EXECUTORS[pool] = executor
        return executor


async def run_bounded[T](
    function: Callable[..., T],
    /,
    *args: object,
    timeout_s: float,
    pool: ExecutorPool = "default",
    **kwargs: object,
) -> T:
    return await _current_executor(pool).run(
        partial(function, *args, **kwargs),
        timeout_s=timeout_s,
    )


def close_bounded_executor() -> None:
    with _GLOBAL_LOCK:
        executors = tuple(_BOUNDED_EXECUTORS.values())
        _BOUNDED_EXECUTORS.clear()
    for executor in executors:
        executor.close()
