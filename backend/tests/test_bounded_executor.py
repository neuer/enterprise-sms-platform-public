from __future__ import annotations

import asyncio
import time
from threading import Event as ThreadEvent

import pytest

from app.core.bounded_executor import (
    BoundedExecutor,
    ExecutorBackpressure,
    close_bounded_executor,
    run_bounded,
)


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


@pytest.mark.asyncio
async def test_slow_ldap_pool_cannot_consume_local_auth_capacity() -> None:
    started = 0
    all_workers_started = asyncio.Event()
    release = ThreadEvent()
    loop = asyncio.get_running_loop()

    def blocking_ldap() -> None:
        nonlocal started
        started += 1
        if started == 4:
            loop.call_soon_threadsafe(all_workers_started.set)
        release.wait(timeout=1)

    ldap_tasks = [
        asyncio.create_task(run_bounded(blocking_ldap, timeout_s=0.1, pool="ldap"))
        for _ in range(8)
    ]
    try:
        await asyncio.wait_for(all_workers_started.wait(), timeout=1)
        assert await run_bounded(lambda: 7, timeout_s=0.1) == 7
    finally:
        release.set()
        await asyncio.gather(*ldap_tasks, return_exceptions=True)
        close_bounded_executor()


@pytest.mark.asyncio
async def test_slow_archive_pool_cannot_consume_local_auth_capacity() -> None:
    started = 0
    all_workers_started = asyncio.Event()
    release = ThreadEvent()
    loop = asyncio.get_running_loop()

    def blocking_archive() -> None:
        nonlocal started
        started += 1
        if started == 4:
            loop.call_soon_threadsafe(all_workers_started.set)
        release.wait(timeout=1)

    archive_tasks = [
        asyncio.create_task(run_bounded(blocking_archive, timeout_s=0.1, pool="archive"))
        for _ in range(8)
    ]
    try:
        await asyncio.wait_for(all_workers_started.wait(), timeout=1)
        assert await run_bounded(lambda: 7, timeout_s=0.1) == 7
    finally:
        release.set()
        await asyncio.gather(*archive_tasks, return_exceptions=True)
        close_bounded_executor()
