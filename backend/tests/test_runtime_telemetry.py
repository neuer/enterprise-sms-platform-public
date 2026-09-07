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

    assert monitor.snapshot().event_loop_delay_peak_seconds >= 0.02
    await monitor.stop()


def test_current_sample_recovers_without_erasing_lifecycle_peak() -> None:
    monitor = EventLoopDelayMonitor()
    monitor.record_delay(0.8)
    first = monitor.snapshot()
    monitor.record_delay(0.002)
    second = monitor.snapshot()
    assert first.event_loop_delay_seconds == 0.8
    assert second.event_loop_delay_seconds == 0.002
    assert second.event_loop_delay_peak_seconds == 0.8
    assert first.process_instance == second.process_instance
    assert EventLoopDelayMonitor().snapshot().process_instance != first.process_instance
