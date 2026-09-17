"""统计报表范围、权限与成功率组合。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol

from app.services.stats import SHANGHAI, success_rate

Granularity = Literal["day", "week", "month"]
GroupBy = Literal["app", "dept"]
ReportCategory = Literal["verify", "notice", "market", "all"]
ReportSort = Literal["period_start", "total", "total_segments", "success_rate"]
ReportOrder = Literal["asc", "desc"]
ReportMetric = Literal["total", "total_segments"]


@dataclass(frozen=True, slots=True)
class ReportingQuery:
    granularity: Granularity
    group_by: GroupBy
    category: ReportCategory
    start: date
    end: date
    scope_dept: str | None
    page: int = 1
    size: int = 20
    sort: ReportSort = "period_start"
    order: ReportOrder = "desc"
    metric: ReportMetric = "total"


@dataclass(frozen=True, slots=True)
class ReportingTotals:
    period_start: date
    dim_value: str
    dim_label: str
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int


@dataclass(frozen=True, slots=True)
class ReportingRow(ReportingTotals):
    success_rate: float


@dataclass(frozen=True, slots=True)
class ReportingSummary:
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int
    success_rate: float


@dataclass(frozen=True, slots=True)
class ReportingDimSummary:
    """维度在整个筛选区间的汇总；success_rate 只经 services/stats.py 计算。"""

    dim_value: str
    dim_label: str
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int
    success_rate: float
    is_other: bool = False


@dataclass(frozen=True, slots=True)
class ReportingTrendSeries:
    dim_value: str
    dim_label: str
    total: tuple[int, ...]
    total_segments: tuple[int, ...]
    is_other: bool


@dataclass(frozen=True, slots=True)
class ReportingTrend:
    periods: tuple[date, ...] = ()
    series: tuple[ReportingTrendSeries, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportingData:
    """仓储返回有界全区间概览、趋势和一页明细，禁止用页数据计算摘要。"""

    dimensions: tuple[ReportingDimSummary, ...]
    trend: ReportingTrend
    items: tuple[ReportingTotals, ...]
    total: int
    dimension_total: int


@dataclass(frozen=True, slots=True)
class ReportingResult:
    granularity: Granularity
    group_by: GroupBy
    category: ReportCategory
    start: date
    end: date
    can_export_decrypted: bool
    summary: ReportingSummary
    dim_summary: tuple[ReportingDimSummary, ...]
    items: tuple[ReportingRow, ...]
    total: int = 0
    page: int = 1
    size: int = 20
    metric: ReportMetric = "total"
    dimension_total: int = 0
    trend: ReportingTrend = field(default_factory=ReportingTrend)


class ReportingRepository(Protocol):
    async def query(self, query: ReportingQuery) -> ReportingData: ...


class ReportingService:
    """限制报表查询成本并固定非高权限用户的部门范围。"""

    def __init__(
        self,
        repository: ReportingRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.clock = clock

    async def get(
        self,
        *,
        granularity: Granularity,
        group_by: GroupBy,
        category: ReportCategory,
        start: date | None,
        end: date | None,
        role: str,
        dept: str,
        page: int = 1,
        size: int = 20,
        sort: ReportSort = "period_start",
        order: ReportOrder = "desc",
        metric: ReportMetric = "total",
    ) -> ReportingResult:
        if not 1 <= page <= 1_000_000 or not 1 <= size <= 100:
            raise ValueError("report page or size is out of bounds")
        if sort not in {"period_start", "total", "total_segments", "success_rate"}:
            raise ValueError("invalid report sort")
        if order not in {"asc", "desc"} or metric not in {"total", "total_segments"}:
            raise ValueError("invalid report order or metric")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reporting clock must be timezone-aware")
        resolved_end = end or now.astimezone(SHANGHAI).date()
        resolved_start = start or resolved_end - timedelta(days=29)
        if resolved_start > resolved_end:
            raise ValueError("report start must not be later than end")
        if (resolved_end - resolved_start).days > 365:
            raise ValueError("report range cannot exceed 366 days")
        query = ReportingQuery(
            granularity,
            group_by,
            category,
            resolved_start,
            resolved_end,
            None if role in {"approver", "admin"} else dept,
            page, size, sort, order, metric,
        )
        data = await self.repository.query(query)
        totals = data.dimensions
        delivered = sum(item.delivered for item in totals)
        failed = sum(item.failed for item in totals)
        return ReportingResult(
            granularity,
            group_by,
            category,
            resolved_start,
            resolved_end,
            role in {"approver", "admin"},
            ReportingSummary(
                sum(item.total for item in totals),
                sum(item.total_segments for item in totals),
                delivered,
                failed,
                sum(item.unknown for item in totals),
                success_rate(delivered, failed),
            ),
            data.dimensions,
            tuple(
                ReportingRow(
                    item.period_start,
                    item.dim_value,
                    item.dim_label,
                    item.total,
                    item.total_segments,
                    item.delivered,
                    item.failed,
                    item.unknown,
                    success_rate(item.delivered, item.failed),
                )
                for item in data.items
            ),
            data.total, page, size, metric, data.dimension_total, data.trend,
        )
