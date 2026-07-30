"""独立 PostgreSQL Outbox dispatcher；不依赖 Celery beat 触发自身。"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

from app.core.runtime_resources import (
    close_runtime_resources,
    configure_runtime_resources,
)
from app.services.alert_repository import SqlAlertService
from app.services.outbox import OutboxDispatcher
from app.services.outbox_queue import CeleryOutboxPublisher
from app.services.outbox_repository import SqlOutboxRepository
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)


async def dispatch_forever(*, idle_seconds: float = 1.0) -> None:
    """持续领取 due 事件；broker 故障只推进持久化重试，不退出或丢事件。"""

    if not 0.1 <= idle_seconds <= 30:
        raise ValueError("invalid outbox idle interval")
    dispatcher = OutboxDispatcher(
        SqlOutboxRepository(pooled=True),
        CeleryOutboxPublisher(),
    )
    repository = dispatcher.repository
    next_monitor_at = monotonic()
    while True:
        try:
            published = await dispatcher.dispatch_once()
        except Exception as exc:
            LOGGER.error(
                "outbox dispatch iteration failed",
                extra={"error_type": type(exc).__name__},
            )
            published = 0
        now = monotonic()
        if now >= next_monitor_at and isinstance(repository, SqlOutboxRepository):
            next_monitor_at = now + 60
            try:
                stats = await repository.stats()
                if stats.dead or stats.oldest_age_seconds >= 300:
                    await SqlAlertService().emit(
                        alert_type="outbox_backlog",
                        level="crit" if stats.dead else "warn",
                        title="事务性 Outbox 存在积压或死信",
                        detail={
                            "pending": stats.pending,
                            "published": stats.published,
                            "processing": stats.processing,
                            "dead": stats.dead,
                            "oldest_age_seconds": stats.oldest_age_seconds,
                        },
                        dedup_key="outbox_backlog",
                        dedup_hours=1,
                    )
            except Exception as exc:
                LOGGER.error(
                    "outbox monitor iteration failed",
                    extra={"error_type": type(exc).__name__},
                )
        if published == 0:
            await asyncio.sleep(idle_seconds)


def main() -> None:
    async def run() -> None:
        configure_runtime_resources(get_settings(), component="background")
        try:
            await dispatch_forever()
        finally:
            await close_runtime_resources()

    asyncio.run(run())


if __name__ == "__main__":
    main()
