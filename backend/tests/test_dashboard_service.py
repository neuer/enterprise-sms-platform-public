from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.jobtrack import JobSpec
from app.services.current_alerts import CurrentAlert, CurrentAlertSnapshot
from app.services.dashboard import (
    CategoryTotals,
    DashboardFacts,
    DashboardOperationsFacts,
    DashboardService,
    JobLatest,
    TrendDayTotals,
)


class FakeRepository:
    def __init__(self, facts: DashboardFacts) -> None:
        self.facts = facts
        self.calls: list[tuple[str | None, date, bool]] = []

    async def load(
        self,
        scope_dept: str | None,
        today: date,
        *,
        include_operations: bool,
    ) -> DashboardFacts:
        self.calls.append((scope_dept, today, include_operations))
        return self.facts


class FakeCurrentAlerts:
    async def get(self) -> CurrentAlertSnapshot:
        return CurrentAlertSnapshot(
            datetime(2026, 7, 12, 4, 0, tzinfo=UTC),
            False,
            ("control_redis",),
            (
                CurrentAlert(
                    "queue_paused",
                    "queue_paused",
                    "crit",
                    "短信发送队列当前处于暂停状态",
                    {},
                    None,
                    datetime(2026, 7, 12, 4, 0, tzinfo=UTC),
                    "queue",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_dashboard_applies_dept_scope_fills_categories_and_calculates_health() -> None:
    now = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
    facts = DashboardFacts(
        categories=(CategoryTotals("notice", 10, 12, 8, 2, 0),),
        pending_approvals=2,
    )
    repository = FakeRepository(facts)
    service = DashboardService(
        repository,
        (JobSpec("poll_report", 60), JobSpec("aggregate_stats", 300)),
        clock=lambda: now,
    )

    result = await service.get(role="viewer", dept="业务一部")

    assert repository.calls == [("业务一部", date(2026, 7, 12), False)]
    assert [item.category for item in result.categories] == ["verify", "notice", "market"]
    assert result.categories[1].success_rate == 0.8
    assert result.overall_success_rate == 0.8
    assert result.categories[0].total == 0
    assert result.operations is None


@pytest.mark.asyncio
async def test_dashboard_pivots_seven_day_trend_and_fills_missing_days() -> None:
    now = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
    facts = DashboardFacts(
        categories=(),
        pending_approvals=0,
        trend=(
            TrendDayTotals(date(2026, 7, 11), "verify", 3),
            TrendDayTotals(date(2026, 7, 11), "market", 1),
            TrendDayTotals(date(2026, 7, 12), "notice", 10),
        ),
    )
    service = DashboardService(
        FakeRepository(facts),
        (),
        clock=lambda: now,
    )

    result = await service.get(role="viewer", dept="业务一部")

    assert [(item.stat_date, item.verify, item.notice, item.market) for item in result.trend] == [
        (date(2026, 7, 6), 0, 0, 0),
        (date(2026, 7, 7), 0, 0, 0),
        (date(2026, 7, 8), 0, 0, 0),
        (date(2026, 7, 9), 0, 0, 0),
        (date(2026, 7, 10), 0, 0, 0),
        (date(2026, 7, 11), 3, 0, 1),
        (date(2026, 7, 12), 0, 10, 0),
    ]


@pytest.mark.asyncio
async def test_elevated_dashboard_is_global_and_failed_or_late_job_is_red() -> None:
    now = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
    facts = DashboardFacts(
        categories=(),
        pending_approvals=0,
        operations=DashboardOperationsFacts(
            current_balance=None,
            balances=(),
            alerts=(),
            uncertain=0,
            unmatched=0,
            callback_dead=0,
            jobs=(JobLatest("poll_report", now - timedelta(seconds=130), "failed"),),
        ),
    )
    repository = FakeRepository(facts)
    service = DashboardService(
        repository,
        (JobSpec("poll_report", 60),),
        clock=lambda: now,
    )

    result = await service.get(role="admin", dept="审批部")

    assert repository.calls == [(None, date(2026, 7, 12), True)]
    assert result.operations is not None
    assert result.operations.jobs[0].stalled is True


@pytest.mark.asyncio
async def test_admin_dashboard_uses_current_alert_snapshot_and_marks_unknown() -> None:
    now = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
    facts = DashboardFacts(
        categories=(),
        pending_approvals=0,
        operations=DashboardOperationsFacts(
            current_balance=None,
            balances=(),
            alerts=(),
            uncertain=0,
            unmatched=0,
            callback_dead=0,
            jobs=(),
        ),
    )
    service = DashboardService(
        FakeRepository(facts),
        (),
        current_alerts=FakeCurrentAlerts(),
        clock=lambda: now,
    )

    result = await service.get(role="admin", dept="平台部")

    assert result.operations is not None
    assert [alert.title for alert in result.operations.alerts] == [
        "当前告警状态不完整",
        "短信发送队列当前处于暂停状态",
    ]
