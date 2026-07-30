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
