from __future__ import annotations

import asyncio

import pytest

import app.core.worker_runtime as worker_runtime


def test_worker_runtime_reuses_one_event_loop_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_on_loops: list[int] = []

    async def close_resources() -> None:
        closed_on_loops.append(id(asyncio.get_running_loop()))

    monkeypatch.setattr(worker_runtime, "close_runtime_resources", close_resources)
    runtime = worker_runtime.WorkerAsyncRuntime()

    async def loop_identity() -> int:
        return id(asyncio.get_running_loop())

    first = runtime.run(loop_identity())
    second = runtime.run(loop_identity())
    runtime.close()

    assert first == second
    assert closed_on_loops == [first]


def test_worker_runtime_propagates_task_error_and_remains_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_runtime,
        "close_runtime_resources",
        _noop_close,
    )
    runtime = worker_runtime.WorkerAsyncRuntime()

    async def fail() -> None:
        raise RuntimeError("worker task failed")

    async def healthy() -> str:
        return "ok"

    try:
        with pytest.raises(RuntimeError, match="worker task failed"):
            runtime.run(fail())
        assert runtime.run(healthy()) == "ok"
    finally:
        runtime.close()


def test_interrupted_result_wait_cancels_orphan_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SoftTimeLimitExceeded 中断主线程等待后，协程必须被取消而非继续执行。"""

    from celery.exceptions import SoftTimeLimitExceeded

    monkeypatch.setattr(worker_runtime, "close_runtime_resources", _noop_close)
    runtime = worker_runtime.WorkerAsyncRuntime()
    outcome: list[str] = []

    class InterruptedFuture:
        """模拟 Celery 软超时信号在 future.result() 阻塞期间抛出。"""

        def __init__(self, inner: object) -> None:
            self.inner = inner
            self.calls = 0

        def result(self, timeout: float | None = None) -> object:
            self.calls += 1
            if self.calls == 1:
                raise SoftTimeLimitExceeded()
            return self.inner.result(timeout)  # type: ignore[attr-defined]

        def cancel(self) -> bool:
            return self.inner.cancel()  # type: ignore[attr-defined]

    real_run_coroutine_threadsafe = asyncio.run_coroutine_threadsafe
    wrapped_once = []

    def interrupted(coroutine: object, loop: object) -> object:
        future = real_run_coroutine_threadsafe(coroutine, loop)  # type: ignore[arg-type]
        if wrapped_once:
            return future
        wrapped_once.append(True)
        return InterruptedFuture(future)

    monkeypatch.setattr(worker_runtime.asyncio, "run_coroutine_threadsafe", interrupted)

    async def hang() -> None:
        try:
            await asyncio.sleep(60)
            outcome.append("completed")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    try:
        with pytest.raises(SoftTimeLimitExceeded):
            runtime.run(hang())
    finally:
        runtime.close()

    assert outcome == ["cancelled"]


def test_worker_runtime_stops_loop_when_resource_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_close() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(worker_runtime, "close_runtime_resources", fail_close)
    runtime = worker_runtime.WorkerAsyncRuntime()
    runtime.start()

    with pytest.raises(RuntimeError, match="resources failed to close"):
        runtime.close()

    assert runtime._thread is None
    assert runtime._loop is None


async def _noop_close() -> None:
    return None
