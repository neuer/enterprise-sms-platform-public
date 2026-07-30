"""只向 Celery 发布无敏感数据的批次引用。"""

from __future__ import annotations

from app.core.bounded_executor import run_bounded
from app.tasks import celery_app


class CeleryQueuePublisher:
    async def enqueue(self, batch_no: str, queue: str) -> None:
        """任务载荷仅含 batch_no；手机号和实际内容均由 worker 从 DB 读取。"""

        await run_bounded(
            celery_app.send_task,
            "app.tasks.send.process_batch",
            args=[batch_no],
            queue=queue,
            ignore_result=True,
            timeout_s=3,
        )

    async def enqueue_chunk(self, chunk_id: int, queue: str) -> None:
        await run_bounded(
            celery_app.send_task,
            "app.tasks.send.process_chunk",
            args=[chunk_id],
            queue=queue,
            ignore_result=True,
            timeout_s=3,
        )
