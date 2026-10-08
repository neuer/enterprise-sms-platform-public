"""发布验证专用的无业务副作用消费任务。"""

from __future__ import annotations

from typing import Any

from app.core.worker_probe import PROBE_TASK, consume_probe
from app.settings import get_settings
from app.tasks import celery_app


@celery_app.task(  # type: ignore[untyped-decorator]
    name=PROBE_TASK,
    bind=True,
    ignore_result=True,
    soft_time_limit=3,
    time_limit=5,
)
def worker_probe(self: Any, nonce: str) -> None:
    """保留 BrokerTask 前置授权，仅响应本职责队列的随机挑战。"""

    consume_probe(self, nonce, get_settings().redis_broker_role)
