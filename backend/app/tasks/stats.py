"""滚动三日 stat_daily 聚合任务。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.stats import StatsAggregationService, StatsRepository
from app.services.stats_repository import SqlStatsRepository
from app.tasks import celery_app


async def aggregate_stats_once(
    repository: StatsRepository,
    *,
    now: datetime | None = None,
) -> int:
    """执行一次滚动统计；入参与返回值均不含 PII。"""

    return await StatsAggregationService(repository).aggregate_recent(now or datetime.now(UTC))


async def _aggregate() -> int:
    return await aggregate_stats_once(SqlStatsRepository())


@celery_app.task(name="app.tasks.aggregate_stats")  # type: ignore[untyped-decorator]
@tracked_job("aggregate_stats", expect_interval_s=300)
def aggregate_stats() -> int:
    """Celery 同步入口，固定投递到 bulk 队列。"""

    return run_worker_async(_aggregate())
