"""Outbox 到 Celery 的唯一发布适配器；task_id 固定为 event ID。"""

from __future__ import annotations

from app.core.bounded_executor import run_bounded
from app.services.outbox import OutboxLease
from app.tasks import celery_app


class CeleryOutboxPublisher:
    async def publish(self, event: OutboxLease) -> None:
        """仅发布已在 PostgreSQL 固化的任务名、稳定引用和事件 ID。"""

        await run_bounded(
            celery_app.send_task,
            event.task_name,
            args=[*event.args, str(event.event_id)],
            queue=event.queue,
            task_id=str(event.event_id),
            headers={
                "correlation_id": str(event.correlation_id or event.event_id),
            },
            ignore_result=True,
            timeout_s=3,
        )
