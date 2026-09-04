"""发送运行时心跳：worker/dispatcher 主动续租，过期即准入失败关闭。"""

from __future__ import annotations

import logging
import sys
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)
HEARTBEAT_LEASE_SECONDS = 30
REQUIRED_COMPONENTS = ("send-realtime", "send-bulk", "outbox-dispatcher")


async def touch_runtime_heartbeat(
    component: str,
    *,
    success: bool = False,
    engine: Any | None = None,
) -> None:
    """写入或续租运行组件心跳；失败只记日志，不得阻断业务提交。"""

    if component not in REQUIRED_COMPONENTS:
        raise ValueError("unknown runtime heartbeat component")
    owned = engine is None
    selected = engine
    try:
        if selected is None:
            selected = database_engine(get_settings().database_url)
        async with selected.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO send_runtime_heartbeat (
                      component, generation, last_heartbeat_at,
                      last_success_at, lease_until
                    ) VALUES (
                      :component, 1, now(),
                      CASE WHEN :success THEN now() ELSE NULL END,
                      now() + make_interval(secs => :lease)
                    )
                    ON CONFLICT (component) DO UPDATE SET
                      generation = send_runtime_heartbeat.generation + 1,
                      last_heartbeat_at = now(),
                      last_success_at = CASE
                        WHEN :success THEN now()
                        ELSE send_runtime_heartbeat.last_success_at
                      END,
                      lease_until = now() + make_interval(secs => :lease)
                    """
                ),
                {
                    "component": component,
                    "success": success,
                    "lease": HEARTBEAT_LEASE_SECONDS,
                },
            )
    except Exception as exc:
        LOGGER.warning(
            "runtime heartbeat unavailable",
            extra={"component": component, "error_type": type(exc).__name__},
        )
    finally:
        if owned and selected is not None:
            await selected.dispose()


def send_worker_heartbeat_components(argv: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """只给实际消费 realtime/bulk 的 worker 续租发送心跳。"""

    selected = " ".join(argv or tuple(sys.argv)).lower()
    if "-q" in selected or "--queues" in selected:
        components: list[str] = []
        if "realtime" in selected:
            components.append("send-realtime")
        if "bulk" in selected:
            components.append("send-bulk")
        return tuple(components)
    return ("send-realtime", "send-bulk")


_HEARTBEATS_STARTED = False


def start_send_worker_heartbeats() -> None:
    """空闲 worker 也必须续租，避免准入把缺失心跳当健康。"""

    global _HEARTBEATS_STARTED
    if _HEARTBEATS_STARTED:
        return
    components = send_worker_heartbeat_components()
    if not components:
        return

    async def beat() -> None:
        for component in components:
            await touch_runtime_heartbeat(component)

    from app.core.worker_runtime import schedule_worker_periodic

    schedule_worker_periodic(beat, HEARTBEAT_LEASE_SECONDS / 3)
    _HEARTBEATS_STARTED = True
