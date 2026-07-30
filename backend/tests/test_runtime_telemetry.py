from __future__ import annotations

import asyncio
import time

import pytest

from app.core.runtime_telemetry import EventLoopDelayMonitor


@pytest.mark.asyncio
async def test_event_loop_monitor_records_blocking_delay_and_stops_cleanly() -> None:
    monitor = EventLoopDelayMonitor(interval_s=0.005)
    monitor.start()
    await asyncio.sleep(0.01)

    time.sleep(0.03)  # noqa: ASYNC251 - 刻意制造事件循环阻塞以验证观测值。
    await asyncio.sleep(0.01)

    assert monitor.snapshot().event_loop_delay_seconds >= 0.02
    await monitor.stop()
