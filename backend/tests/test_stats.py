from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.stats import StatsAggregationService, recent_stat_dates, success_rate


class FakeRepository:
    def __init__(self, dirty: tuple[date, ...] = ()) -> None:
        self.dates: list[date] = []
        self.dirty = dirty
        self.cleared: list[date] = []

    async def aggregate_day(self, stat_date: date) -> int:
        self.dates.append(stat_date)
        return 2

    async def list_dirty_dates(self) -> tuple[date, ...]:
        return self.dirty

    async def clear_dirty_date(self, stat_date: date) -> None:
        self.cleared.append(stat_date)


def test_success_rate_has_one_unknown_excluding_definition() -> None:
    assert success_rate(8, 2) == 0.8
    assert success_rate(0, 0) == 0.0
    with pytest.raises(ValueError):
        success_rate(-1, 0)


def test_recent_dates_follow_shanghai_calendar_boundary() -> None:
    now = datetime(2026, 7, 11, 16, 5, tzinfo=UTC)
    assert recent_stat_dates(now) == (
        date(2026, 7, 12),
        date(2026, 7, 11),
        date(2026, 7, 10),
        date(2026, 7, 9),
        date(2026, 7, 8),
    )
    with pytest.raises(ValueError):
        recent_stat_dates(datetime(2026, 7, 12, 0, 0))


@pytest.mark.asyncio
async def test_aggregation_service_rebuilds_three_dates_and_returns_row_count() -> None:
    repository = FakeRepository()
    service = StatsAggregationService(repository)

    assert await service.aggregate_recent(datetime(2026, 7, 11, 16, 5, tzinfo=UTC)) == 10
    assert repository.dates == [
        date(2026, 7, 12),
        date(2026, 7, 11),
        date(2026, 7, 10),
        date(2026, 7, 9),
        date(2026, 7, 8),
    ]


@pytest.mark.asyncio
async def test_dirty_dates_outside_window_are_recomputed_then_cleared() -> None:
    """晚到回执标记的窗口外归属日必须补算；窗口内脏日只清除不重复计算。"""

    late_day = date(2026, 6, 20)
    in_window_day = date(2026, 7, 10)
    repository = FakeRepository(dirty=(late_day, in_window_day))
    service = StatsAggregationService(repository)

    assert await service.aggregate_recent(datetime(2026, 7, 11, 16, 5, tzinfo=UTC)) == 12
    assert repository.dates == [
        date(2026, 7, 12),
        date(2026, 7, 11),
        date(2026, 7, 10),
        date(2026, 7, 9),
        date(2026, 7, 8),
        late_day,
    ]
    assert repository.cleared == [late_day, in_window_day]
