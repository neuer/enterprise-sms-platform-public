"""从权威运行事实实时派生当前告警，不从历史告警时间窗猜测状态。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from app.core.jobtrack import (
    JobRunSnapshot,
    JobSpec,
    consecutive_failed_count,
    job_stalled_since,
)
from app.services.outbox import OUTBOX_BACKLOG_ALERT_SECONDS
from app.services.runtime_policy import RuntimePolicy

AlertLevel = Literal["info", "warn", "crit"]
CurrentAlertTarget = Literal["jobs", "raw", "uncertain", "callbacks", "queue", "outbox"]


@dataclass(frozen=True, slots=True)
class CurrentAlert:
    key: str
    alert_type: str
    level: AlertLevel
    title: str
    detail: dict[str, Any]
    since: datetime | None
    checked_at: datetime
    target: CurrentAlertTarget


@dataclass(frozen=True, slots=True)
class CurrentAlertSnapshot:
    refreshed_at: datetime
    complete: bool
    unknown_sources: tuple[str, ...]
    items: tuple[CurrentAlert, ...]


@dataclass(frozen=True, slots=True)
class CurrentJobFact:
    job_name: str
    latest: JobRunSnapshot | None
    recent_statuses: tuple[str, ...]
    latest_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class UsageDriftFact:
    kind: str
    mismatched_dimensions: int
    absolute_delta: int
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class RawSpillAlertFact:
    alert_type: str
    source: Literal["report", "reply"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DatabaseCurrentFacts:
    policy: RuntimePolicy
    jobs: tuple[CurrentJobFact, ...]
    usage_drift: tuple[UsageDriftFact, ...]
    balance: int | None
    balance_checked_at: datetime | None
    uncertain_overdue: int
    uncertain_since: datetime | None
    callback_dead: int
    callback_dead_since: datetime | None
    outbox_dead: int
    outbox_dead_since: datetime | None
    outbox_active: int
    outbox_oldest_active_at: datetime | None
    raw_manual: int
    raw_manual_since: datetime | None
    raw_spill_alerts: tuple[RawSpillAlertFact, ...]


@dataclass(frozen=True, slots=True)
class ControlCurrentFacts:
    realtime_pause_code: str | None
    bulk_pause_code: str | None
    vendor_consecutive_failures: int


class CurrentAlertRepository(Protocol):
    async def load_database(
        self,
        specs: tuple[JobSpec, ...],
    ) -> DatabaseCurrentFacts: ...

    async def load_control(self) -> ControlCurrentFacts: ...


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("current alert time must be timezone-aware")
    return moment


class CurrentAlertService:
    """并行读取 PostgreSQL 与 control Redis；单域失败时保留其余可信结果。"""

    def __init__(
        self,
        repository: CurrentAlertRepository,
        specs: tuple[JobSpec, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.specs = tuple(sorted(specs, key=lambda item: item.job_name))
        self.clock = clock

    def _job_spec(self, name: str, fallback_seconds: int) -> JobSpec:
        return next(
            (spec for spec in self.specs if spec.job_name == name),
            JobSpec(name, fallback_seconds),
        )

    async def get(self) -> CurrentAlertSnapshot:
        now = _aware(self.clock())
        database_result, control_result = await asyncio.gather(
            self.repository.load_database(self.specs),
            self.repository.load_control(),
            return_exceptions=True,
        )
        unknown: list[str] = []
        items: list[CurrentAlert] = []

        if isinstance(database_result, BaseException):
            if isinstance(database_result, asyncio.CancelledError):
                raise database_result
            unknown.append("postgresql")
        else:
            self._append_database_items(database_result, items, unknown, now)

        if isinstance(control_result, BaseException):
            if isinstance(control_result, asyncio.CancelledError):
                raise control_result
            unknown.append("control_redis")
        else:
            self._append_control_items(control_result, items, now)

        severity = {"crit": 0, "warn": 1, "info": 2}
        items.sort(
            key=lambda item: (
                severity[item.level],
                (item.since or now).timestamp(),
                item.key,
            )
        )
        sources = tuple(dict.fromkeys(unknown))
        return CurrentAlertSnapshot(now, not sources, sources, tuple(items))

    def _append_database_items(
        self,
        facts: DatabaseCurrentFacts,
        items: list[CurrentAlert],
        unknown: list[str],
        now: datetime,
    ) -> None:
        jobs = {fact.job_name: fact for fact in facts.jobs}
        for spec in self.specs:
            fact = jobs.get(spec.job_name, CurrentJobFact(spec.job_name, None, (), None))
            stalled_since = job_stalled_since(fact.latest, spec, now=now)
            if fact.latest is None or stalled_since is not None:
                items.append(
                    CurrentAlert(
                        f"job_stalled:{spec.job_name}",
                        "job_stalled",
                        "warn",
                        f"后台任务心跳缺失：{spec.job_name}",
                        {
                            "job_name": spec.job_name,
                            "expect_interval_s": spec.expect_interval_s,
                        },
                        stalled_since,
                        now,
                        "jobs",
                    )
                )
            failures = consecutive_failed_count(fact.recent_statuses)
            if failures >= 3:
                items.append(
                    CurrentAlert(
                        f"job_failed:{spec.job_name}",
                        "job_failed",
                        "crit",
                        f"后台任务连续失败：{spec.job_name}",
                        {"job_name": spec.job_name, "consecutive_failures": failures},
                        fact.latest.started_at if fact.latest is not None else None,
                        now,
                        "jobs",
                    )
                )

        usage_by_kind = {item.kind: item for item in facts.usage_drift}
        usage_spec = self._job_spec("reconcile_usage_projection", 300)
        usage_rows = [usage_by_kind.get("quota"), usage_by_kind.get("frequency")]
        usage_stale = any(
            row is None
            or now - _aware(row.checked_at)
            > timedelta(seconds=usage_spec.expect_interval_s * 2)
            for row in usage_rows
        )
        if usage_stale:
            unknown.append("usage_projection")
        else:
            checked_rows = [row for row in usage_rows if row is not None]
            mismatches = sum(row.mismatched_dimensions for row in checked_rows)
            delta = sum(row.absolute_delta for row in checked_rows)
            if mismatches or delta:
                items.append(
                    CurrentAlert(
                        "usage_projection_drift",
                        "usage_projection_drift",
                        "crit",
                        "配额或频控投影与事实账本不一致",
                        {
                            "mismatched_dimensions": mismatches,
                            "absolute_delta": delta,
                        },
                        min(row.checked_at for row in checked_rows),
                        min(row.checked_at for row in checked_rows),
                        "jobs",
                    )
                )

        balance_spec = self._job_spec("poll_balance", 600)
        if (
            facts.balance is None
            or facts.balance_checked_at is None
            or now - _aware(facts.balance_checked_at)
            > timedelta(seconds=balance_spec.expect_interval_s * 2)
        ):
            unknown.append("balance")
        elif facts.balance < facts.policy.balance_alert_threshold:
            items.append(
                CurrentAlert(
                    "balance_low",
                    "balance_low",
                    "warn",
                    "短信厂商余额低于阈值",
                    {
                        "balance": facts.balance,
                        "threshold": facts.policy.balance_alert_threshold,
                    },
                    facts.balance_checked_at,
                    facts.balance_checked_at,
                    "queue",
                )
            )

        if facts.uncertain_overdue:
            items.append(
                CurrentAlert(
                    "uncertain_overdue",
                    "uncertain_overdue",
                    "crit",
                    "存在超过告警时限的发送结果未知分片",
                    {
                        "count": facts.uncertain_overdue,
                        "threshold_hours": facts.policy.uncertain_alert_hours,
                    },
                    facts.uncertain_since,
                    now,
                    "uncertain",
                )
            )
        if facts.callback_dead:
            items.append(
                CurrentAlert(
                    "callback_dead",
                    "callback_dead",
                    "crit",
                    "存在重试耗尽的结果回调",
                    {"count": facts.callback_dead},
                    facts.callback_dead_since,
                    now,
                    "callbacks",
                )
            )

        outbox_old = (
            facts.outbox_oldest_active_at is not None
            and now - _aware(facts.outbox_oldest_active_at)
            >= timedelta(seconds=OUTBOX_BACKLOG_ALERT_SECONDS)
        )
        if facts.outbox_dead or outbox_old:
            items.append(
                CurrentAlert(
                    "outbox_backlog",
                    "outbox_backlog",
                    "crit" if facts.outbox_dead else "warn",
                    "事务性 Outbox 存在积压或死信",
                    {
                        "active": facts.outbox_active,
                        "dead": facts.outbox_dead,
                        "alert_after_seconds": OUTBOX_BACKLOG_ALERT_SECONDS,
                    },
                    facts.outbox_dead_since
                    if facts.outbox_dead
                    else facts.outbox_oldest_active_at,
                    now,
                    "outbox",
                )
            )
        if facts.raw_manual:
            items.append(
                CurrentAlert(
                    "vendor_raw_oversized_complete",
                    "vendor_raw_oversized_complete",
                    "crit",
                    "存在完整但超过自动解析上限的厂商报文",
                    {"count": facts.raw_manual},
                    facts.raw_manual_since,
                    now,
                    "raw",
                )
            )

        latest_success = {fact.job_name: fact.latest_success_at for fact in facts.jobs}
        raw_titles = {
            "vendor_raw_spill_failed": "raw spill 写盘能力尚未由后续成功轮询证明恢复",
            "vendor_raw_spill_quota_exceeded": "raw spill 容量阻断尚未由后续成功轮询证明恢复",
        }
        for alert in facts.raw_spill_alerts:
            job_name = "poll_report" if alert.source == "report" else "poll_reply"
            recovered_at = latest_success.get(job_name)
            if recovered_at is not None and _aware(recovered_at) > _aware(alert.created_at):
                continue
            items.append(
                CurrentAlert(
                    f"{alert.alert_type}:{alert.source}",
                    alert.alert_type,
                    "crit",
                    raw_titles[alert.alert_type],
                    {"source": alert.source},
                    alert.created_at,
                    now,
                    "jobs",
                )
            )

    @staticmethod
    def _append_control_items(
        facts: ControlCurrentFacts,
        items: list[CurrentAlert],
        now: datetime,
    ) -> None:
        if facts.realtime_pause_code or facts.bulk_pause_code:
            items.append(
                CurrentAlert(
                    "queue_paused",
                    "queue_paused",
                    "crit",
                    "短信发送队列当前处于暂停状态",
                    {
                        "realtime_code": facts.realtime_pause_code,
                        "bulk_code": facts.bulk_pause_code,
                    },
                    None,
                    now,
                    "queue",
                )
            )
        if facts.vendor_consecutive_failures >= 3:
            items.append(
                CurrentAlert(
                    "vendor_consecutive_failure",
                    "vendor_consecutive_failure",
                    "crit",
                    "短信厂商接口连续失败",
                    {"consecutive_failures": facts.vendor_consecutive_failures},
                    None,
                    now,
                    "jobs",
                )
            )
