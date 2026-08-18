from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.stats import StatsAggregationService, recent_stat_dates, success_rate


class FakeRepository:
    def __init__(self) -> None:
        self.dates: list[date] = []

    async def aggregate_day(self, stat_date: date) -> int:
        self.dates.append(stat_date)
        return 2


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
