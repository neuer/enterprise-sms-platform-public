"""Celery prefork 子进程的持久 asyncio loop 与共享异步资源边界。"""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Coroutine
from concurrent.futures import Future
from threading import Event, Lock, Thread
from typing import Any, TypeVar

from app.core.runtime_resources import close_runtime_resources

T = TypeVar("T")


class WorkerAsyncRuntime:
    """让同一 worker 子进程的所有同步 Celery task 共用一个事件循环。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self._lock = Lock()

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
        future: Future[T] = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result()

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            loop = self._loop
            self._thread = None
            self._loop = None
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


def start_worker_runtime() -> None:
    _WORKER_RUNTIME.start()


def close_worker_runtime() -> None:
    _WORKER_RUNTIME.close()


atexit.register(close_worker_runtime)
