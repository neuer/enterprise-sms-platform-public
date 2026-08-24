"""Prometheus 平台聚合指标的领域快照与文本渲染。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from app.core.runtime_telemetry import (
    RuntimeTelemetrySnapshot,
    runtime_telemetry_snapshot,
)
from app.vendor.codes import ERROR_POLICIES

CATEGORIES = ("verify", "notice", "market")
QUEUES = ("realtime", "bulk")
RAW_REPLAY_ELIGIBILITIES = ("automatic", "manual", "never")
VENDOR_ERROR_LABELS = frozenset(str(code) for code in ERROR_POLICIES)
LEASE_EVENT_LABELS = frozenset(
    {
        "acquired",
        "takeover",
        "heartbeat_lost",
        "fencing_miss",
        "manual_retry",
        "dead",
    }
)


@dataclass(frozen=True, slots=True)
class MetricsFacts:
    """PostgreSQL 提供的无 PII 平台级聚合事实。"""

    send_rates: tuple[tuple[str, float], ...]
    vendor_errors: tuple[tuple[str, int], ...]
    uncertain: int
    callback_failures: tuple[tuple[str, int], ...]
    frequency_filtered: tuple[tuple[str, int], ...]
    poll_lags: tuple[tuple[str, float], ...]
    usage_projection_mismatches: tuple[tuple[str, int], ...] = ()
    usage_projection_absolute_delta: tuple[tuple[str, int], ...] = ()
    worker_stalled_leases: tuple[tuple[str, int], ...] = ()
    worker_lease_events: tuple[tuple[str, str, int], ...] = ()
    queue_depths: tuple[tuple[str, int], ...] = ()
    raw_replay_eligibility: tuple[tuple[str, int], ...] = ()
    system_replay_audit_pending: int = 0


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """一次 scrape 使用的 Redis 与 PostgreSQL 一致视图。"""

    queue_depths: tuple[tuple[str, int], ...]
    facts: MetricsFacts
    runtime: RuntimeTelemetrySnapshot | None = None
    snapshot_age_seconds: float = 0.0


class MetricsRepository(Protocol):
    async def load(self) -> MetricsFacts: ...


class MetricsService:
    """从 PostgreSQL 权威事实生成指标，API 不持有 broker 凭据。"""

    def __init__(
        self,
        repository: MetricsRepository,
        *,
        runtime: Callable[[], RuntimeTelemetrySnapshot] = runtime_telemetry_snapshot,
        clock: Callable[[], float] = monotonic,
        collection_timeout_s: float = 2.0,
        snapshot_ttl_s: float = 15.0,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.clock = clock
        self.collection_timeout_s = collection_timeout_s
        self.snapshot_ttl_s = snapshot_ttl_s
        self._refresh_lock = asyncio.Lock()
        self._cached: MetricsSnapshot | None = None
        self._cached_at: float | None = None

    async def collect(self) -> MetricsSnapshot:
        """以有界超时和 single-flight 刷新快照；失败时绝不返回旧值。"""

        now = self.clock()
        if (
            self._cached is not None
            and self._cached_at is not None
            and now - self._cached_at < self.snapshot_ttl_s
        ):
            return replace(
                self._cached,
                snapshot_age_seconds=max(0.0, now - self._cached_at),
            )
        async with self._refresh_lock:
            now = self.clock()
            if (
                self._cached is not None
                and self._cached_at is not None
                and now - self._cached_at < self.snapshot_ttl_s
            ):
                return replace(
                    self._cached,
                    snapshot_age_seconds=max(0.0, now - self._cached_at),
                )
            async with asyncio.timeout(self.collection_timeout_s):
                facts = await self.repository.load()
            snapshot = MetricsSnapshot(
                queue_depths=facts.queue_depths,
                facts=facts,
                runtime=self.runtime(),
            )
            self._cached = snapshot
            self._cached_at = self.clock()
            return snapshot


def _values(items: tuple[tuple[str, float | int], ...]) -> dict[str, float]:
    return {label: float(value) for label, value in items}


def render_prometheus(snapshot: MetricsSnapshot) -> bytes:
    """使用请求级 registry 渲染固定低基数指标，杜绝跨请求残值。"""

    registry = CollectorRegistry()
    snapshot_age = Gauge(
        "sms_metrics_snapshot_age_seconds",
        "Age of the successfully collected dependency snapshot.",
        registry=registry,
    )
    snapshot_age.set(max(0.0, snapshot.snapshot_age_seconds))
    queue_depth = Gauge(
        "sms_queue_depth",
        "Celery business queue depth.",
        ("queue",),
        registry=registry,
    )
    for queue, value in snapshot.queue_depths:
        queue_depth.labels(queue=queue).set(value)

    send_rate = Gauge(
        "sms_send_rate_per_second",
        "Recipient messages submitted to vendor per second over five minutes.",
        ("category",),
        registry=registry,
    )
    send_values = _values(snapshot.facts.send_rates)
    for category in CATEGORIES:
        send_rate.labels(category=category).set(send_values.get(category, 0.0))

    vendor_errors = Gauge(
        "sms_vendor_error_chunks",
        "Current failed or retrying chunks grouped by vendor code.",
        ("code",),
        registry=registry,
    )
    normalized_errors: dict[str, int] = {}
    for code, error_count in snapshot.facts.vendor_errors:
        label = code if code in VENDOR_ERROR_LABELS else "other"
        normalized_errors[label] = normalized_errors.get(label, 0) + error_count
    for code, error_count in sorted(normalized_errors.items()):
        vendor_errors.labels(code=code).set(error_count)

    uncertain = Gauge(
        "sms_uncertain_chunks",
        "Current chunks with an uncertain vendor submission result.",
        registry=registry,
    )
    uncertain.set(snapshot.facts.uncertain)

    callback_failures = Gauge(
        "sms_callback_failures",
        "Current callback tasks that are retrying or dead.",
        ("status",),
        registry=registry,
    )
    callback_values = _values(snapshot.facts.callback_failures)
    for status in ("retrying", "dead"):
        callback_failures.labels(status=status).set(callback_values.get(status, 0.0))

    frequency_filtered = Gauge(
        "sms_frequency_filtered_messages",
        "Messages removed by frequency limits during the current Shanghai day.",
        ("category",),
        registry=registry,
    )
    frequency_values = _values(snapshot.facts.frequency_filtered)
    for category in CATEGORIES:
        frequency_filtered.labels(category=category).set(
            frequency_values.get(category, 0.0)
        )

    poll_lag = Gauge(
        "sms_poll_lag_seconds",
        "Seconds since the latest successful vendor poll.",
        ("source",),
        registry=registry,
    )
    for source, lag_seconds in snapshot.facts.poll_lags:
        poll_lag.labels(source=source).set(lag_seconds)

    projection_mismatches = Gauge(
        "sms_usage_projection_drift_dimensions",
        "Redis usage projection dimensions that differ from PostgreSQL facts.",
        ("kind",),
        registry=registry,
    )
    mismatch_values = _values(snapshot.facts.usage_projection_mismatches)
    projection_delta = Gauge(
        "sms_usage_projection_drift_absolute_delta",
        "Absolute Redis/PostgreSQL usage projection delta.",
        ("kind",),
        registry=registry,
    )
    delta_values = _values(snapshot.facts.usage_projection_absolute_delta)
    for kind in ("quota", "frequency"):
        projection_mismatches.labels(kind=kind).set(mismatch_values.get(kind, 0.0))
        projection_delta.labels(kind=kind).set(delta_values.get(kind, 0.0))

    stalled_leases = Gauge(
        "sms_worker_stalled_leases",
        "Expired callback/export execution leases awaiting takeover.",
        ("task_kind",),
        registry=registry,
    )
    stalled_values = _values(snapshot.facts.worker_stalled_leases)
    for task_kind in ("callback", "export"):
        stalled_leases.labels(task_kind=task_kind).set(
            stalled_values.get(task_kind, 0.0)
        )

    lease_events = Gauge(
        "sms_worker_lease_events",
        "Persisted callback/export lease lifecycle events.",
        ("task_kind", "event"),
        registry=registry,
    )
    normalized_lease_events: dict[tuple[str, str], int] = {}
    for task_kind, event, count in snapshot.facts.worker_lease_events:
        task_label = task_kind if task_kind in {"callback", "export"} else "other"
        event_label = event if event in LEASE_EVENT_LABELS else "other"
        key = (task_label, event_label)
        normalized_lease_events[key] = normalized_lease_events.get(key, 0) + count
    for (task_kind, event), count in sorted(normalized_lease_events.items()):
        lease_events.labels(task_kind=task_kind, event=event).set(count)

    eligibility = Gauge(
        "sms_raw_replay_eligibility",
        "raw_vendor_log rows grouped by persisted replay eligibility.",
        ("eligibility",),
        registry=registry,
    )
    eligibility_values = _values(snapshot.facts.raw_replay_eligibility)
    for label in RAW_REPLAY_ELIGIBILITIES:
        eligibility.labels(eligibility=label).set(eligibility_values.get(label, 0.0))

    pending_audit = Gauge(
        "sms_raw_system_replay_audit_pending",
        "processed raw rows still waiting for a system replay audit rewrite.",
        registry=registry,
    )
    pending_audit.set(max(0, snapshot.facts.system_replay_audit_pending))

    if snapshot.runtime is not None:
        loop_delay = Gauge(
            "sms_runtime_event_loop_delay_seconds",
            "Most recently observed API event loop scheduling delay.",
            registry=registry,
        )
        loop_delay.set(snapshot.runtime.event_loop_delay_seconds)
        resident_memory = Gauge(
            "sms_runtime_process_resident_memory_bytes",
            "API process resident memory high-water mark in bytes.",
            registry=registry,
        )
        resident_memory.set(snapshot.runtime.resident_memory_bytes)
        database_connections = Gauge(
            "sms_runtime_database_connections",
            "API process database pool connections.",
            ("state",),
            registry=registry,
        )
        database_connections.labels(state="open").set(
            snapshot.runtime.resources.database_open
        )
        database_connections.labels(state="checked_out").set(
            snapshot.runtime.resources.database_checked_out
        )
        database_pool_connections = Gauge(
            "sms_database_pool_connections",
            "Process database pool connections by bounded component.",
            ("component", "state"),
            registry=registry,
        )
        database_pool_budget = Gauge(
            "sms_database_pool_budget",
            "Maximum process database connections by bounded component.",
            ("component",),
            registry=registry,
        )
        database_pool_acquisitions = Gauge(
            "sms_database_pool_acquisitions_total",
            "Database connection acquisitions observed by component.",
            ("component",),
            registry=registry,
        )
        database_pool_wait = Gauge(
            "sms_database_pool_wait_seconds_total",
            "Cumulative database connection acquisition wait by component.",
            ("component",),
            registry=registry,
        )
        database_pool_timeouts = Gauge(
            "sms_database_pool_timeouts_total",
            "Database pool acquisition timeouts observed by component.",
            ("component",),
            registry=registry,
        )
        database_pool_leaks = Gauge(
            "sms_database_pool_leaked_connections_total",
            "Connections still checked out when a component shut down.",
            ("component",),
            registry=registry,
        )
        for component in snapshot.runtime.resources.database_components:
            database_pool_connections.labels(
                component=component.component,
                state="open",
            ).set(component.open)
            database_pool_connections.labels(
                component=component.component,
                state="checked_out",
            ).set(component.checked_out)
            database_pool_budget.labels(component=component.component).set(
                component.budget
            )
            database_pool_acquisitions.labels(component=component.component).set(
                component.acquisitions
            )
            database_pool_wait.labels(component=component.component).set(
                component.wait_seconds
            )
            database_pool_timeouts.labels(component=component.component).set(
                component.timeouts
            )
            database_pool_leaks.labels(component=component.component).set(
                component.leaked_on_shutdown
            )
        redis_connections = Gauge(
            "sms_runtime_redis_connections",
            "API process Redis pool connections.",
            ("state",),
            registry=registry,
        )
        redis_connections.labels(state="open").set(
            snapshot.runtime.resources.redis_open
        )
        redis_connections.labels(state="in_use").set(
            snapshot.runtime.resources.redis_in_use
        )

    return generate_latest(registry)
