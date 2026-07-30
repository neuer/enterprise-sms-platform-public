from __future__ import annotations

import asyncio
import time
from threading import Event as ThreadEvent

import pytest

from app.core.bounded_executor import BoundedExecutor, ExecutorBackpressure


@pytest.mark.asyncio
async def test_executor_applies_backpressure_and_keeps_event_loop_responsive() -> None:
    executor = BoundedExecutor(max_workers=1, max_pending=1)
    started = asyncio.Event()
    release = ThreadEvent()
    loop = asyncio.get_running_loop()

    def blocking_work() -> str:
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=1)
        return "done"

    first = asyncio.create_task(executor.run(blocking_work, timeout_s=1))
    await started.wait()

    with pytest.raises(ExecutorBackpressure):
        await executor.run(lambda: None, timeout_s=1)

    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 5

    release.set()
    assert await first == "done"
    executor.close()


@pytest.mark.asyncio
async def test_executor_timeout_does_not_release_slot_before_thread_finishes() -> None:
    executor = BoundedExecutor(max_workers=1, max_pending=1)

    with pytest.raises(TimeoutError, match="允许时限"):
        await executor.run(time.sleep, 0.05, timeout_s=0.005)
    with pytest.raises(ExecutorBackpressure):
        await executor.run(lambda: None, timeout_s=1)

    await asyncio.sleep(0.06)
    assert await executor.run(lambda: 7, timeout_s=1) == 7
    executor.close()
