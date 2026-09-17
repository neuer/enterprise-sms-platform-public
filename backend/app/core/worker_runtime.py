"""Celery prefork 子进程的持久 asyncio loop 与共享异步资源边界。"""

from __future__ import annotations

import asyncio
import atexit
import logging
from collections.abc import Coroutine
from concurrent.futures import Future
from contextlib import suppress
from threading import Event, Lock, Thread
from typing import Any, TypeVar

from app.core.runtime_resources import close_runtime_resources

T = TypeVar("T")
CANCELLATION_DRAIN_SECONDS = 10.0
LOGGER = logging.getLogger(__name__)


class WorkerCancellationPending(RuntimeError):
    """取消尚未完成，准入锁必须保持到原协程退出。"""

    def __init__(self, completion: Future[None]) -> None:
        super().__init__("worker coroutine cancellation is still pending")
        self.completion = completion


class WorkerAsyncRuntime:
    """让同一 worker 子进程的所有同步 Celery task 共用一个事件循环。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self._lock = Lock()
        self._periodic: list[Future[Any]] = []
        self._deferred: set[Future[None]] = set()

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._ready.clear()
            thread = Thread(
                target=self._serve,
                name="sms-worker-async-runtime",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("worker async runtime failed to start")

    def run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        self.start()
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("worker async runtime is unavailable")
        task: asyncio.Task[T] | None = None

        async def invoke() -> T:
            nonlocal task
            task = asyncio.current_task()
            return await coroutine

        async def drain() -> None:
            # cancel Future 只通知取消，不代表 loop 上的 finally 已执行。
            await asyncio.sleep(0)
            if task is None:
                coroutine.close()
                return
            with suppress(BaseException):
                await task

        future: Future[T] = asyncio.run_coroutine_threadsafe(invoke(), loop)
        try:
            return future.result()
        except BaseException:
            # SoftTimeLimitExceeded 等在主线程中断等待时，取消 loop 线程上的
            # 协程并有界等待其退出：孤儿协程会与重试投递的同一 chunk 并发，
            # 或在 worker 回收时被硬杀在厂商调用中途。已取消的发送分片由
            # reconcile 的 stale-submitting 扫描按规则 4 转 uncertain。
            future.cancel()
            completion = asyncio.run_coroutine_threadsafe(drain(), loop)
            try:
                completion.result(timeout=CANCELLATION_DRAIN_SECONDS)
            except TimeoutError as error:
                raise WorkerCancellationPending(completion) from error
            raise

    def defer_cleanup(
        self, pending: WorkerCancellationPending | None, coroutine: Coroutine[Any, Any, None]
    ) -> None:
        """软超时后仍在收尾的工作持有原准入锁，真正退出后再释放。"""

        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("worker async runtime is unavailable")

        async def finish() -> None:
            try:
                if pending is not None:
                    await asyncio.wrap_future(pending.completion)
                await coroutine
            finally:
                coroutine.close()

        future = asyncio.run_coroutine_threadsafe(finish(), loop)
        with self._lock:
            self._deferred.add(future)

        def completed(item: Future[None]) -> None:
            with self._lock:
                self._deferred.discard(item)
            if not item.cancelled():
                error = item.exception()
                if error is not None:
                    LOGGER.error(
                        "worker_deferred_cleanup_failed", extra={"error_type": type(error).__name__}
                    )

        future.add_done_callback(completed)

    def schedule_periodic(
        self,
        factory: Any,
        interval_s: float,
    ) -> None:
        """在 worker 常驻 loop 上周期执行；失败不得阻断发送。"""

        self.start()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("worker async runtime is unavailable")

        async def runner() -> None:
            while True:
                with suppress(Exception):
                    await factory()
                await asyncio.sleep(interval_s)

        self._periodic.append(asyncio.run_coroutine_threadsafe(runner(), loop))

    def close(self) -> None:
        with self._lock:
            periodic = list(self._periodic)
            self._periodic.clear()
            periodic.extend(self._deferred)
            self._deferred.clear()
            thread = self._thread
            loop = self._loop
            self._thread = None
            self._loop = None
        for item in periodic:
            item.cancel()
        if thread is None or loop is None:
            return
        close_error: BaseException | None = None
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    close_runtime_resources(),
                    loop,
                ).result(timeout=30)
            except BaseException as error:
                close_error = error
            finally:
                loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("worker async runtime did not stop")
        if close_error is not None:
            raise RuntimeError("worker resources failed to close") from close_error


_WORKER_RUNTIME = WorkerAsyncRuntime()


def run_worker_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return _WORKER_RUNTIME.run(coroutine)


def defer_worker_cleanup(
    pending: WorkerCancellationPending | None, coroutine: Coroutine[Any, Any, None]
) -> None:
    _WORKER_RUNTIME.defer_cleanup(pending, coroutine)


def start_worker_runtime() -> None:
    _WORKER_RUNTIME.start()


def schedule_worker_periodic(factory: Any, interval_s: float) -> None:
    _WORKER_RUNTIME.schedule_periodic(factory, interval_s)


def close_worker_runtime() -> None:
    _WORKER_RUNTIME.close()


atexit.register(close_worker_runtime)
