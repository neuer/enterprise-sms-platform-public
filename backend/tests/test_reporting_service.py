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
    assert result.dim_summary[0].dim_value == "7"
    assert result.dim_summary[0].success_rate == 0.8


@pytest.mark.asyncio
async def test_dim_summary_aggregates_periods_and_orders_by_total() -> None:
    """维度汇总跨周期加总，成功率沿用 stats.py 口径，按消息数降序。"""

    class MultiDimRepository:
        async def query(self, _query: ReportingQuery) -> tuple[ReportingTotals, ...]:
            return (
                ReportingTotals(date(2026, 7, 1), "7", "OA应用", 10, 12, 8, 2, 1),
                ReportingTotals(date(2026, 7, 2), "7", "OA应用", 6, 7, 4, 1, 0),
                ReportingTotals(date(2026, 7, 1), "9", "营销平台", 20, 26, 15, 5, 2),
                ReportingTotals(date(2026, 7, 2), "3", "客服系统", 0, 0, 0, 0, 0),
            )

    service = ReportingService(MultiDimRepository())
    result = await service.get(
        granularity="day",
        group_by="app",
        category="all",
        start=date(2026, 7, 1),
        end=date(2026, 7, 2),
        role="admin",
        dept="平台部",
    )

    dims = result.dim_summary
    assert [item.dim_value for item in dims] == ["9", "7", "3"]
    marketing = dims[0]
    assert (marketing.total, marketing.total_segments) == (20, 26)
    assert (marketing.delivered, marketing.failed, marketing.unknown) == (15, 5, 2)
    assert marketing.success_rate == 15 / 20
    oa = dims[1]
    assert (oa.total, oa.delivered, oa.failed, oa.unknown) == (16, 12, 3, 1)
    assert oa.success_rate == 12 / 15
    # 全量失败的维度 success_rate 为 0，与 stats.success_rate 零分母语义一致
    assert dims[2].success_rate == 0.0
    # 维度汇总与整体摘要来自同一批聚合行
    assert sum(item.total for item in dims) == result.summary.total


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
