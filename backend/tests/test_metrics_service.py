from __future__ import annotations

import asyncio

import pytest
from app.core.runtime_resources import (
    DatabaseComponentSnapshot,
    RuntimeResourceSnapshot,
)
from app.core.runtime_telemetry import RuntimeTelemetrySnapshot
from app.services.metrics import (
    MetricsFacts,
    MetricsService,
    MetricsSnapshot,
    render_prometheus,
)


class FakeRepository:
    def __init__(self, facts: MetricsFacts | None = None, error: Exception | None = None) -> None:
        self.facts = facts
        self.error = error
        self.calls = 0

    async def load(self) -> MetricsFacts:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.facts is not None
        return self.facts


def sample_facts() -> MetricsFacts:
    return MetricsFacts(
        send_rates=(("verify", 1.5), ("market", 0.25)),
        vendor_errors=(("999", 2), ("5002", 3), ("unbounded-code", 7)),
        uncertain=4,
        uncertain_lifecycle=(
            ("active", 1),
            ("overdue", 3),
            ("unknown_terminal", 2),
            ("manual_resolved", 1),
            ("late_evidence", 1),
        ),
        callback_failures=(("retrying", 5), ("dead", 6)),
        frequency_filtered=(("verify", 7), ("market", 8)),
        poll_lags=(("report", 12.5),),
        worker_stalled_leases=(("callback", 2), ("export", 1)),
        worker_lease_events=(
            ("callback", "fencing_miss", 3),
            ("export", "takeover", 4),
            ("unexpected-kind", "free-form-event", 5),
        ),
        queue_depths=(("realtime", 3), ("bulk", 9)),
        raw_replay_eligibility=(("automatic", 2), ("manual", 3), ("never", 5)),
    )


@pytest.mark.asyncio
async def test_collect_uses_durable_queue_depths_without_broker_access() -> None:
    repository = FakeRepository(sample_facts())

    snapshot = await MetricsService(repository).collect()

    assert snapshot.queue_depths == (("realtime", 3), ("bulk", 9))
    assert snapshot.facts == sample_facts()
    assert repository.calls == 1


@pytest.mark.asyncio
async def test_collect_does_not_hide_repository_failure() -> None:
    repository = FakeRepository(error=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await MetricsService(repository).collect()


@pytest.mark.asyncio
async def test_concurrent_scrapes_share_one_bounded_snapshot_refresh() -> None:
    repository = FakeRepository(sample_facts())
    service = MetricsService(
        repository,
        snapshot_ttl_s=15,
    )

    snapshots = await asyncio.gather(*(service.collect() for _ in range(10)))

    assert repository.calls == 1
    assert all(
        snapshot.queue_depths == (("realtime", 3), ("bulk", 9))
        for snapshot in snapshots
    )


@pytest.mark.asyncio
async def test_refresh_timeout_fails_without_returning_stale_snapshot() -> None:
    class BlockingRepository(FakeRepository):
        async def load(self) -> MetricsFacts:
            self.calls += 1
            await asyncio.sleep(1)
            return sample_facts()

    service = MetricsService(
        BlockingRepository(),
        collection_timeout_s=0.001,
        snapshot_ttl_s=1,
    )

    with pytest.raises(TimeoutError):
        await service.collect()


def test_render_exposes_fixed_low_cardinality_metrics_and_zero_categories() -> None:
    body = render_prometheus(
        MetricsSnapshot(
            queue_depths=(("realtime", 3), ("bulk", 9)),
            facts=sample_facts(),
            runtime=RuntimeTelemetrySnapshot(
                0.025,
                RuntimeResourceSnapshot(
                    8,
                    3,
                    5,
                    2,
                    (
                        DatabaseComponentSnapshot(
                            "api",
                            8,
                            3,
                            10,
                            42,
                            1.25,
                            2,
                            1,
                        ),
                    ),
                ),
                123_456,
            ),
        )
    ).decode()

    assert "# TYPE sms_queue_depth gauge" in body
    assert 'sms_send_admission{state="open"} 1.0' in body
    assert 'sms_send_admission{state="closed"} 0.0' in body
    assert "sms_outbox_oldest_age_seconds 0.0" in body
    assert 'sms_send_submit_outcome{result="submitted"} 0.0' in body
    assert 'sms_send_submit_outcome{result="uncertain"} 0.0' in body
    assert "sms_metrics_snapshot_age_seconds 0.0" in body
    assert 'sms_queue_depth{queue="realtime"} 3.0' in body
    assert 'sms_queue_depth{queue="bulk"} 9.0' in body
    assert 'sms_send_rate_per_second{category="verify"} 1.5' in body
    assert 'sms_send_rate_per_second{category="notice"} 0.0' in body
    assert 'sms_vendor_error_chunks{code="999"} 2.0' in body
    assert 'sms_vendor_error_chunks{code="other"} 7.0' in body
    assert "unbounded-code" not in body
    assert "sms_uncertain_chunks 4.0" in body
    assert (
        'sms_uncertain_effect{action="resend_new_batch",result="applied",'
        'source_channel="web"} 0.0'
        in body
    )
    assert 'sms_uncertain_effect_usage_subject_error{kind="other"} 0.0' in body
    assert "sms_uncertain_effect_oldest_pending_seconds 0.0" in body
    assert "sms_uncertain_effect_child_recovered 0.0" in body
    assert 'sms_uncertain_lifecycle_chunks{state="active"} 1.0' in body
    assert 'sms_uncertain_lifecycle_chunks{state="overdue"} 3.0' in body
    assert 'sms_uncertain_lifecycle_chunks{state="unknown_terminal"} 2.0' in body
    assert 'sms_uncertain_lifecycle_chunks{state="manual_resolved"} 1.0' in body
    assert 'sms_uncertain_lifecycle_chunks{state="late_evidence"} 1.0' in body
    assert 'sms_callback_failures{status="retrying"} 5.0' in body
    assert 'sms_frequency_filtered_messages{category="market"} 8.0' in body
    assert 'sms_frequency_filtered_messages{category="notice"} 0.0' in body
    assert 'sms_poll_lag_seconds{source="report"} 12.5' in body
    assert 'sms_poll_lag_seconds{source="reply"}' not in body
    assert 'sms_worker_stalled_leases{task_kind="callback"} 2.0' in body
    assert (
        'sms_worker_lease_events{event="fencing_miss",task_kind="callback"} 3.0'
        in body
    )
    assert 'sms_worker_lease_events{event="other",task_kind="other"} 5.0' in body
    assert 'sms_raw_replay_eligibility{eligibility="automatic"} 2.0' in body
    assert 'sms_raw_replay_eligibility{eligibility="manual"} 3.0' in body
    assert 'sms_raw_replay_eligibility{eligibility="never"} 5.0' in body
    assert "sms_runtime_process_resident_memory_bytes 123456.0" in body
    assert "sms_runtime_event_loop_delay_seconds 0.025" in body
    assert 'sms_runtime_database_connections{state="open"} 8.0' in body
    assert 'sms_runtime_database_connections{state="checked_out"} 3.0' in body
    assert (
        'sms_database_pool_connections{component="api",state="open"} 8.0'
        in body
    )
    assert 'sms_database_pool_budget{component="api"} 10.0' in body
    assert 'sms_database_pool_acquisitions_total{component="api"} 42.0' in body
    assert 'sms_database_pool_wait_seconds_total{component="api"} 1.25' in body
    assert 'sms_database_pool_timeouts_total{component="api"} 2.0' in body
    assert 'sms_database_pool_leaked_connections_total{component="api"} 1.0' in body
    assert 'sms_runtime_redis_connections{state="open"} 5.0' in body
    assert 'sms_runtime_redis_connections{state="in_use"} 2.0' in body
    assert "phone" not in body.casefold()
    assert "content" not in body.casefold()
