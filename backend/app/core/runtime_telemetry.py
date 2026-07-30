"""API 进程事件循环与资源使用的低基数运行指标。"""

from __future__ import annotations

import asyncio
import resource
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from app.core.runtime_resources import RuntimeResourceSnapshot, resource_snapshot


@dataclass(frozen=True, slots=True)
class RuntimeTelemetrySnapshot:
    event_loop_delay_seconds: float
    resources: RuntimeResourceSnapshot
    resident_memory_bytes: int = 0


def _resident_memory_bytes() -> int:
    usage = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return usage if sys.platform == "darwin" else usage * 1024


class EventLoopDelayMonitor:
    """用单调时钟记录最近一次调度延迟，不创建外部依赖。"""

    def __init__(self, *, interval_s: float = 0.5) -> None:
        if interval_s <= 0:
            raise ValueError("event loop monitor interval must be positive")
        self.interval_s = interval_s
        self.last_delay_seconds = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + self.interval_s
            await asyncio.sleep(self.interval_s)
            self.last_delay_seconds = max(
                self.last_delay_seconds,
                max(0.0, loop.time() - expected),
            )

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("event loop monitor already started")
        self._task = asyncio.create_task(
            self._run(),
            name="runtime-event-loop-delay",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def snapshot(self) -> RuntimeTelemetrySnapshot:
        return RuntimeTelemetrySnapshot(
            self.last_delay_seconds,
            resource_snapshot(),
            _resident_memory_bytes(),
        )


_MONITOR = EventLoopDelayMonitor()


def runtime_telemetry_snapshot() -> RuntimeTelemetrySnapshot:
    return _MONITOR.snapshot()


def create_runtime_monitor(
    factory: Callable[[], EventLoopDelayMonitor] = EventLoopDelayMonitor,
) -> EventLoopDelayMonitor:
    """应用生命周期创建独立 monitor，测试与多 app 进程互不复用任务。"""

    global _MONITOR
    _MONITOR = factory()
    return _MONITOR
