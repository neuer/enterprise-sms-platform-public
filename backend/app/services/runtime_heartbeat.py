"""发送运行时心跳：worker/dispatcher 主动续租，过期即准入失败关闭。"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)
HEARTBEAT_LEASE_SECONDS = 30
REQUIRED_COMPONENTS = ("send-realtime", "send-bulk", "outbox-dispatcher")
KNOWN_COMPONENTS = frozenset(REQUIRED_COMPONENTS)
QUEUE_COMPONENTS = {
    "realtime": "send-realtime",
    "bulk": "send-bulk",
}
EXPLICIT_NONE = frozenset({"", "none"})


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


def parse_heartbeat_components(raw: str) -> tuple[str, ...]:
    """解析显式心跳能力；未知值失败关闭。"""

    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items or all(item.lower() in EXPLICIT_NONE for item in items):
        return ()
    unknown = [item for item in items if item not in KNOWN_COMPONENTS]
    if unknown:
        raise ValueError("unknown runtime heartbeat component")
    return tuple(dict.fromkeys(items))


def celery_queue_names(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """语法级解析 Celery 队列参数；未声明队列时返回 None。"""

    found = False
    queues: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"-Q", "--queues"}:
            found = True
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                queues.extend(
                    part.strip() for part in argv[index + 1].split(",") if part.strip()
                )
                index += 2
                continue
            index += 1
            continue
        if item.startswith("--queues="):
            found = True
            queues.extend(
                part.strip()
                for part in item.split("=", 1)[1].split(",")
                if part.strip()
            )
        elif item.startswith("-Q") and item != "-Q":
            found = True
            queues.extend(part.strip() for part in item[2:].split(",") if part.strip())
        index += 1
    if not found:
        return None
    return tuple(queues)


def send_worker_heartbeat_components(
    argv: tuple[str, ...] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """按显式能力或精确队列名续租；禁止 realtime-report 子串误认。"""

    selected_env = os.environ if environ is None else environ
    explicit = selected_env.get("SMS_RUNTIME_HEARTBEAT_COMPONENTS")
    if explicit is not None:
        return parse_heartbeat_components(explicit)

    queues = celery_queue_names(argv if argv is not None else tuple(sys.argv))
    if queues is not None:
        return tuple(
            dict.fromkeys(
                QUEUE_COMPONENTS[name] for name in queues if name in QUEUE_COMPONENTS
            )
        )
    if selected_env.get("ENVIRONMENT") == "production":
        return ()
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
