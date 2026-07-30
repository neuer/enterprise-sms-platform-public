from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest

from app.core.jobtrack import JOB_SPECS, JobSpec
from app.tasks.scheduler import build_beat_schedule
from app.tasks.stats import aggregate_stats, aggregate_stats_once


class FakeRepository:
    def __init__(self) -> None:
        self.dates: list[date] = []

    async def aggregate_day(self, stat_date: date) -> int:
        self.dates.append(stat_date)
        return 1


@pytest.mark.asyncio
async def test_stats_task_adapter_uses_recent_three_day_service() -> None:
    repository = FakeRepository()
    assert await aggregate_stats_once(repository) == 3
    assert len(repository.dates) == 3


def test_stats_task_is_tracked_and_scheduled_on_bulk_queue() -> None:
    assert cast(Any, aggregate_stats).name == "app.tasks.aggregate_stats"
    assert JOB_SPECS["aggregate_stats"] == JobSpec("aggregate_stats", 300)
    assert build_beat_schedule({})["aggregate-stats"] == {
        "task": "app.tasks.aggregate_stats",
        "schedule": 300,
        "options": {"queue": "bulk"},
    }
