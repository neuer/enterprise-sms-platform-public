from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.reporting import (
    ReportingQuery,
    ReportingService,
    ReportingTotals,
)


class FakeRepository:
    def __init__(self) -> None:
        self.queries: list[ReportingQuery] = []

    async def query(self, query: ReportingQuery) -> tuple[ReportingTotals, ...]:
        self.queries.append(query)
        return (
            ReportingTotals(date(2026, 7, 1), "7", "OA应用", 10, 12, 8, 2, 1),
        )


@pytest.mark.asyncio
async def test_defaults_to_recent_thirty_shanghai_days_and_dept_scope() -> None:
    repository = FakeRepository()
    service = ReportingService(
        repository,
        clock=lambda: datetime(2026, 7, 12, 3, 0, tzinfo=UTC),
    )

    result = await service.get(
        granularity="day",
        group_by="app",
        category="all",
        start=None,
        end=None,
        role="viewer",
        dept="业务一部",
    )

    assert repository.queries == [
        ReportingQuery(
            "day", "app", "all", date(2026, 6, 13), date(2026, 7, 12), "业务一部"
        )
    ]
    assert result.items[0].success_rate == 0.8
    assert result.summary.total_segments == 12
    assert result.summary.success_rate == 0.8
    assert result.can_export_decrypted is False


@pytest.mark.asyncio
async def test_elevated_scope_and_date_range_validation() -> None:
    repository = FakeRepository()
    service = ReportingService(repository)
    elevated = await service.get(
        granularity="month",
        group_by="dept",
        category="market",
        start=date(2026, 1, 1),
        end=date(2026, 7, 12),
        role="admin",
        dept="平台部",
    )
    assert repository.queries[0].scope_dept is None
    assert elevated.can_export_decrypted is True

    with pytest.raises(ValueError, match="366"):
        await service.get(
            granularity="day",
            group_by="app",
            category="all",
            start=date(2025, 1, 1),
            end=date(2026, 7, 12),
            role="viewer",
            dept="业务一部",
        )
    with pytest.raises(ValueError, match="later"):
        await service.get(
            granularity="week",
            group_by="dept",
            category="notice",
            start=date(2026, 7, 13),
            end=date(2026, 7, 12),
            role="viewer",
            dept="业务一部",
        )
