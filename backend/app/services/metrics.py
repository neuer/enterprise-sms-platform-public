"""Prometheus 平台聚合指标的领域快照与文本渲染。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from app.core.auth.observability import auth_observability_snapshot
from app.core.runtime_telemetry import (
    RuntimeTelemetrySnapshot,
    runtime_telemetry_snapshot,
)
from app.vendor.codes import ERROR_POLICIES

CATEGORIES = ("verify", "notice", "market")
QUEUES = ("realtime", "bulk")
RAW_REPLAY_ELIGIBILITIES = ("automatic", "manual", "never")
UNCERTAIN_LIFECYCLE_STATES = (
    "active",
    "overdue",
    "unknown_terminal",
    "manual_resolved",
    "late_evidence",
)
SEND_ADMISSION_STATES = ("open", "degraded", "closed")
SEND_SUBMIT_OUTCOMES = (
    "submitted",
    "retry_scheduled",
    "delayed",
    "paused",
    "rejected",
    "stale",
    "failed",
    "uncertain",
    "split",
)
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
    uncertain_lifecycle: tuple[tuple[str, int], ...] = ()
    usage_projection_mismatches: tuple[tuple[str, int], ...] = ()
    usage_projection_absolute_delta: tuple[tuple[str, int], ...] = ()
    worker_stalled_leases: tuple[tuple[str, int], ...] = ()
    worker_lease_events: tuple[tuple[str, str, int], ...] = ()
    queue_depths: tuple[tuple[str, int], ...] = ()
    raw_replay_eligibility: tuple[tuple[str, int], ...] = ()
    system_replay_audit_pending: int = 0
    send_admission_state: str = "open"
    outbox_oldest_age_seconds: float = 0.0
    send_submit_outcomes: tuple[tuple[str, int], ...] = ()


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

    admission = Gauge(
        "sms_send_admission",
        "Send admission capacity band inferred from Outbox and overdue facts.",
        ("state",),
        registry=registry,
    )
    current_state = snapshot.facts.send_admission_state
    if current_state not in SEND_ADMISSION_STATES:
        current_state = "closed"
    for state in SEND_ADMISSION_STATES:
        admission.labels(state=state).set(1.0 if state == current_state else 0.0)
    oldest_age = Gauge(
        "sms_outbox_oldest_age_seconds",
        "Age in seconds of the oldest active Outbox event.",
        registry=registry,
    )
    oldest_age.set(max(0.0, snapshot.facts.outbox_oldest_age_seconds))

    submit_outcomes = Gauge(
        "sms_send_submit_outcome",
        "Vendor submit() diagnostic counts by low-cardinality outcome.",
        ("result",),
        registry=registry,
    )
    outcome_values = _values(snapshot.facts.send_submit_outcomes)
    for result in SEND_SUBMIT_OUTCOMES:
        submit_outcomes.labels(result=result).set(outcome_values.get(result, 0.0))

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

    lifecycle = Gauge(
        "sms_uncertain_lifecycle_chunks",
        "Uncertain lifecycle counts by active, overdue, terminal, resolved and late evidence.",
        ("state",),
        registry=registry,
    )
    lifecycle_values = _values(snapshot.facts.uncertain_lifecycle)
    for state in UNCERTAIN_LIFECYCLE_STATES:
        lifecycle.labels(state=state).set(lifecycle_values.get(state, 0.0))

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

    auth = auth_observability_snapshot()
    created = Gauge(
        "auth_transition_created_total",
        "Auth lock/ban transitions that attempted persistent audit.",
        ("action",),
        registry=registry,
    )
    success = Gauge(
        "auth_transition_audit_success_total",
        "Auth lock/ban transitions whose audit insert succeeded or deduped.",
        ("action",),
        registry=registry,
    )
    failure = Gauge(
        "auth_transition_audit_failure_total",
        "Auth lock/ban transitions whose audit insert failed closed.",
        ("action",),
        registry=registry,
    )
    for action, value in auth.transition_created:
        created.labels(action=action).set(value)
    for action, value in auth.transition_success:
        success.labels(action=action).set(value)
    for action, value in auth.transition_failure:
        failure.labels(action=action).set(value)
    pending = Gauge(
        "auth_transition_pending",
        "Auth lock/ban transitions waiting for durable audit.",
        registry=registry,
    )
    pending.set(auth.transition_pending)
    oldest = Gauge(
        "auth_transition_oldest_pending_seconds",
        "Age of the oldest pending auth transition.",
        registry=registry,
    )
    oldest.set(auth.transition_oldest_pending_seconds)
    claims = Gauge(
        "auth_transition_claim_total",
        "Writer lease claims for auth transitions.",
        ("action", "owner"),
        registry=registry,
    )
    for action, owner, value in auth.transition_claim:
        claims.labels(action=action, owner=owner).set(value)
    expired = Gauge(
        "auth_transition_lease_expired_total",
        "Auth transition writer leases that expired before ACK.",
        ("action",),
        registry=registry,
    )
    for action, value in auth.transition_lease_expired:
        expired.labels(action=action).set(value)
    retries = Gauge(
        "auth_transition_retry_total",
        "Auth transition writer retries after a failed audit write.",
        ("action", "error_class"),
        registry=registry,
    )
    for action, error_class, value in auth.transition_retry:
        retries.labels(action=action, error_class=error_class).set(value)
    dead = Gauge(
        "auth_transition_dead_total",
        "Auth transitions that exhausted the documented recovery limit.",
        ("action",),
        registry=registry,
    )
    for action, value in auth.transition_dead:
        dead.labels(action=action).set(value)
    duration = Gauge(
        "auth_transition_database_duration_seconds",
        "Last auth transition audit write duration.",
        ("action",),
        registry=registry,
    )
    for action, seconds in auth.transition_database_duration_seconds:
        duration.labels(action=action).set(seconds)
    admit = Gauge(
        "auth_admit_total",
        "Login admit outcomes on the Redis-first path.",
        ("outcome",),
        registry=registry,
    )
    for outcome, value in auth.admit:
        admit.labels(outcome=outcome).set(value)
    policy_hit = Gauge(
        "auth_policy_cache_hit_total",
        "Auth guard policy cache hits.",
        registry=registry,
    )
    policy_hit.set(auth.policy_cache_hit)
    policy_miss = Gauge(
        "auth_policy_cache_miss_total",
        "Auth guard policy cache misses.",
        registry=registry,
    )
    policy_miss.set(auth.policy_cache_miss)
    policy_failure = Gauge(
        "auth_policy_load_failure_total",
        "Auth guard policy load failures.",
        registry=registry,
    )
    policy_failure.set(auth.policy_load_failure)
    policy_age = Gauge(
        "auth_policy_snapshot_age_seconds",
        "Age of the last usable auth guard policy snapshot.",
        registry=registry,
    )
    policy_age.set(auth.policy_snapshot_age_seconds)
    guard_queries = Gauge(
        "auth_guard_db_queries_total",
        "PostgreSQL queries issued by the auth guard policy loader.",
        registry=registry,
    )
    guard_queries.set(auth.guard_db_queries)
    session_revision = Gauge(
        "auth_session_policy_revision",
        "Authoritative AD session policy revision by source.",
        ("source",),
        registry=registry,
    )
    for source, value in auth.session_policy_revision:
        if source in {"postgres", "redis"}:
            session_revision.labels(source=source).set(value)
    session_publish = Gauge(
        "auth_session_policy_publish_total",
        "AD session policy Redis CAS publish outcomes.",
        ("outcome",),
        registry=registry,
    )
    for outcome, value in auth.session_policy_publish:
        session_publish.labels(outcome=outcome).set(value)
    session_reconcile = Gauge(
        "auth_session_policy_reconcile_total",
        "AD session policy reconciler outcomes.",
        ("outcome",),
        registry=registry,
    )
    for outcome, value in auth.session_policy_reconcile:
        session_reconcile.labels(outcome=outcome).set(value)
    session_conflict = Gauge(
        "auth_session_policy_conflict_total",
        "AD session policy version conflicts.",
        ("type",),
        registry=registry,
    )
    for conflict_type, value in auth.session_policy_conflict:
        session_conflict.labels(type=conflict_type).set(value)
    session_age = Gauge(
        "auth_session_policy_snapshot_age_seconds",
        "Age of the last usable AD session policy Redis snapshot.",
        registry=registry,
    )
    session_age.set(auth.session_policy_snapshot_age_seconds)
    session_lag = Gauge(
        "auth_session_policy_publish_lag_seconds",
        "PostgreSQL minus Redis AD session policy revision lag.",
        registry=registry,
    )
    session_lag.set(auth.session_policy_publish_lag_seconds)

    return generate_latest(registry)
