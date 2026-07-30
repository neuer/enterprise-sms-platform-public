"""运维动作写入 PostgreSQL Outbox，API 无需接触 Celery broker。"""

from __future__ import annotations

from uuid import uuid4

from app.core.runtime_resources import database_engine
from app.services.outbox import MANUAL_JOB_TASK_NAMES, OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.settings import Settings, get_settings


class OutboxJobSender:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send(self, task_name: str, queue: str) -> None:
        if task_name not in MANUAL_JOB_TASK_NAMES:
            raise ValueError("manual job task is not allowlisted")
        job_name = task_name.rsplit(".", 1)[-1]
        request_id = uuid4()
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="job.trigger",
                        aggregate_type="job",
                        aggregate_id=job_name,
                        task_name="app.tasks.outbox.trigger_job",
                        queue=queue,
                        args=(task_name,),
                        dedup_key=f"job.trigger:{job_name}:{request_id}",
                    ),
                )
        finally:
            await engine.dispose()


class OutboxBatchSender:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send_batch(self, batch_no: str, lane: str) -> None:
        request_id = uuid4()
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="batch.ready",
                        aggregate_type="sms_batch",
                        aggregate_id=batch_no,
                        task_name="app.tasks.send.process_batch",
                        queue=lane,
                        args=(batch_no,),
                        dedup_key=f"batch.ready:{request_id.hex}",
                    ),
                )
        finally:
            await engine.dispose()
