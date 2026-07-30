"""仪表盘快照的权限、成功率与任务健康组合。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol

from app.core.jobtrack import JobSpec
from app.services.stats import SHANGHAI, success_rate

Category = Literal["verify", "notice", "market"]


@dataclass(frozen=True, slots=True)
class CategoryTotals:
    category: Category
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int


@dataclass(frozen=True, slots=True)
class CategoryMetric(CategoryTotals):
    success_rate: float


@dataclass(frozen=True, slots=True)
class BalancePoint:
    stat_date: date
    balance: int


@dataclass(frozen=True, slots=True)
class AlertSummary:
    level: str
    title: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobLatest:
    job_name: str
    last_run_at: datetime
    last_status: str


@dataclass(frozen=True, slots=True)
class JobHealth:
    job_name: str
    last_run_at: datetime | None
    last_status: str | None
    stalled: bool


@dataclass(frozen=True, slots=True)
class ChannelMonitor:
    realtime_queue: int | None
    bulk_queue: int | None
    qps_used: int | None
    qps_rate: int
    reserved_realtime_qps: int
    stale: bool


@dataclass(frozen=True, slots=True)
class DashboardUiPolicy:
    test_send_max: int


@dataclass(frozen=True, slots=True)
class DashboardOperationsFacts:
    current_balance: int | None
    balances: tuple[BalancePoint, ...]
    alerts: tuple[AlertSummary, ...]
    uncertain: int
    unmatched: int
    callback_dead: int
    jobs: tuple[JobLatest, ...]
    realtime_queue: int | None = None
    bulk_queue: int | None = None
    qps_used: int | None = None
    qps_rate: int = 5
    reserved_realtime_qps: int = 2
    channel_stale: bool = True
    balance_alert_threshold: int = 10000


@dataclass(frozen=True, slots=True)
class DashboardOperations:
    current_balance: int | None
    balances: tuple[BalancePoint, ...]
    alerts: tuple[AlertSummary, ...]
    uncertain: int
    unmatched: int
    callback_dead: int
    jobs: tuple[JobHealth, ...]
    realtime_queue: int | None = None
    bulk_queue: int | None = None
    qps_used: int | None = None
    qps_rate: int = 5
    reserved_realtime_qps: int = 2
    channel_stale: bool = True
    balance_alert_threshold: int = 10000


@dataclass(frozen=True, slots=True)
class DashboardFacts:
    categories: tuple[CategoryTotals, ...]
    pending_approvals: int
    test_send_max: int = 5
    operations: DashboardOperationsFacts | None = None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    refreshed_at: datetime
    categories: tuple[CategoryMetric, ...]
    overall_success_rate: float
    pending_approvals: int
    ui_policy: DashboardUiPolicy = DashboardUiPolicy(5)
    operations: DashboardOperations | None = None


class DashboardRepository(Protocol):
    async def load(
        self,
        scope_dept: str | None,
        today: date,
        *,
        include_operations: bool,
    ) -> DashboardFacts: ...


class DashboardService:
    """以 JWT 部门范围读取事实并在服务端统一派生展示指标。"""

    def __init__(
        self,
        repository: DashboardRepository,
        job_specs: tuple[JobSpec, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.job_specs = job_specs
        self.clock = clock

    async def get(self, *, role: str, dept: str) -> DashboardSnapshot:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("dashboard clock must be timezone-aware")
        today = now.astimezone(SHANGHAI).date()
        facts = await self.repository.load(
            None if role in {"approver", "admin"} else dept,
            today,
            include_operations=role == "admin",
        )
        totals = {item.category: item for item in facts.categories}
        categories: list[CategoryMetric] = []
        for category in ("verify", "notice", "market"):
            category_totals = totals.get(
                category,
                CategoryTotals(category, 0, 0, 0, 0, 0),
            )
            categories.append(
                CategoryMetric(
                    category_totals.category,
                    category_totals.total,
                    category_totals.total_segments,
                    category_totals.delivered,
                    category_totals.failed,
                    category_totals.unknown,
                    success_rate(category_totals.delivered, category_totals.failed),
                )
            )
        operation_facts = facts.operations
        operations: DashboardOperations | None = None
        if operation_facts is not None:
            latest = {item.job_name: item for item in operation_facts.jobs}
            jobs: list[JobHealth] = []
            for spec in self.job_specs:
                job_latest = latest.get(spec.job_name)
                stalled = (
                    job_latest is None
                    or job_latest.last_status != "success"
                    or now - job_latest.last_run_at
                    > timedelta(seconds=spec.expect_interval_s * 2)
                )
                jobs.append(
                    JobHealth(
                        spec.job_name,
                        job_latest.last_run_at if job_latest else None,
                        job_latest.last_status if job_latest else None,
                        stalled,
                    )
                )
            operations = DashboardOperations(
                operation_facts.current_balance,
                operation_facts.balances,
                operation_facts.alerts,
                operation_facts.uncertain,
                operation_facts.unmatched,
                operation_facts.callback_dead,
                tuple(jobs),
                operation_facts.realtime_queue,
                operation_facts.bulk_queue,
                operation_facts.qps_used,
                operation_facts.qps_rate,
                operation_facts.reserved_realtime_qps,
                operation_facts.channel_stale,
                operation_facts.balance_alert_threshold,
            )
        return DashboardSnapshot(
            now,
            tuple(categories),
            success_rate(
                sum(item.delivered for item in categories),
                sum(item.failed for item in categories),
            ),
            facts.pending_approvals,
            DashboardUiPolicy(facts.test_send_max),
            operations,
        )
