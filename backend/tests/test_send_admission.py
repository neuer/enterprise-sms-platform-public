from __future__ import annotations

from typing import Any

import pytest

from app.services.send_admission import (
    SendAdmissionFacts,
    SendAdmissionGuard,
    SendAdmissionRejected,
    SendAdmissionUnavailable,
    decide,
    evaluate_capacity,
)
from app.services.send_admission_repository import SqlSendAdmissionRepository


def facts(**overrides: Any) -> SendAdmissionFacts:
    values: dict[str, Any] = {
        "outbox_active": 0,
        "outbox_oldest_age_s": 0,
        "outbox_dead": 0,
        "uncertain_overdue": 0,
        "callback_dead": 0,
        "realtime_paused": False,
        "bulk_paused": False,
        "vendor_failures": 0,
    }
    values.update(overrides)
    return SendAdmissionFacts(**values)


def test_capacity_bands_use_outbox_not_broker() -> None:
    assert evaluate_capacity(facts()) == ("open", "ok")
    assert evaluate_capacity(facts(outbox_active=200)) == ("degraded", "outbox_backlog")
    assert evaluate_capacity(facts(outbox_active=2000)) == ("closed", "outbox_backlog")
    assert evaluate_capacity(facts(outbox_oldest_age_s=300)) == ("degraded", "outbox_oldest")
    assert evaluate_capacity(facts(outbox_oldest_age_s=600)) == ("closed", "outbox_oldest")
    assert evaluate_capacity(facts(outbox_dead=10)) == ("degraded", "outbox_dead")
    assert evaluate_capacity(facts(outbox_dead=50)) == ("closed", "outbox_dead")
    assert evaluate_capacity(facts(uncertain_overdue=20)) == ("degraded", "uncertain_overdue")
    assert evaluate_capacity(facts(callback_dead=20)) == ("degraded", "callback_backlog")
    assert evaluate_capacity(facts(callback_dead=200)) == ("closed", "callback_backlog")
    assert evaluate_capacity(facts(vendor_failures=3)) == ("degraded", "vendor_failures")
    assert evaluate_capacity(facts(realtime_paused=True)) == ("degraded", "queue_paused")
    assert evaluate_capacity(facts(realtime_paused=True, bulk_paused=True)) == (
        "closed",
        "queues_paused",
    )
    assert evaluate_capacity(facts(heartbeat_stale=True)) == ("closed", "heartbeat_stale")


def test_recovery_hysteresis_holds_closed_then_degraded() -> None:
    recovering = facts(outbox_active=1200)
    assert evaluate_capacity(recovering) == ("degraded", "outbox_backlog")
    assert evaluate_capacity(recovering, previous_state="closed") == (
        "closed",
        "recovery_hold",
    )
    ramp = facts(outbox_active=120)
    assert evaluate_capacity(ramp, previous_state="closed") == (
        "degraded",
        "recovery_hold",
    )
    assert evaluate_capacity(facts(outbox_active=50), previous_state="degraded") == (
        "open",
        "ok",
    )


def test_degraded_allows_small_notice_but_rejects_market_and_bulk() -> None:
    backlog = facts(outbox_active=200)
    allowed = decide(backlog, category="notice", recipient_count=20)
    assert allowed.allowed is True
    assert allowed.state == "degraded"
    market = decide(backlog, category="market", recipient_count=1)
    assert market.allowed is False
    assert market.reason == "degraded_bulk"
    assert market.retry_after_s == 30
    large = decide(backlog, category="verify", recipient_count=21)
    assert large.allowed is False
    assert large.reason == "degraded_volume"


def test_lane_pause_closes_only_that_category() -> None:
    realtime = facts(realtime_paused=True)
    notice = decide(realtime, category="notice", recipient_count=1)
    assert notice.allowed is False
    assert notice.reason == "realtime_paused"
    market = decide(realtime, category="market", recipient_count=1)
    assert market.allowed is False
    assert market.reason == "degraded_bulk"
    bulk_only = facts(bulk_paused=True)
    assert decide(bulk_only, category="verify", recipient_count=1).allowed is True
    assert decide(bulk_only, category="market", recipient_count=1).reason == "bulk_paused"
    both = decide(
        facts(realtime_paused=True, bulk_paused=True),
        category="verify",
        recipient_count=1,
    )
    assert both.allowed is False
    assert both.reason == "queues_paused"


class FakeFacts:
    def __init__(
        self,
        payload: SendAdmissionFacts | None = None,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.loads = 0
        self.transitions: list[dict[str, object]] = []

    async def load(self) -> SendAdmissionFacts:
        self.loads += 1
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return self.payload

    async def record_transition(self, **values: object) -> None:
        self.transitions.append(values)


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        return self.values.pop(0)


@pytest.mark.asyncio
async def test_guard_reuses_fresh_snapshot_and_versions_on_refresh() -> None:
    repo = FakeFacts(facts(outbox_active=10))
    clock = SequenceClock([0.0, 0.0, 1.0, 6.0, 6.0])
    guard = SendAdmissionGuard(repo, clock=clock)
    first = await guard.snapshot()
    second = await guard.snapshot()
    assert first.version == second.version == 1
    assert repo.loads == 1
    repo.payload = facts(outbox_active=2000)
    third = await guard.snapshot()
    assert third.version == 2
    assert third.state == "closed"
    assert repo.loads == 2
    assert repo.transitions[-1]["state"] == "closed"


@pytest.mark.asyncio
async def test_expired_snapshot_does_not_fail_open() -> None:
    repo = FakeFacts(facts())
    clock = SequenceClock([0.0, 0.0, 6.0, 6.0])
    guard = SendAdmissionGuard(repo, clock=clock)
    await guard.authorize(category="notice", channel="api", recipient_count=1)
    repo.error = RuntimeError("control plane down")
    with pytest.raises(SendAdmissionUnavailable) as error:
        await guard.authorize(category="notice", channel="api", recipient_count=1)
    assert error.value.reason == "snapshot_unavailable"
    assert error.value.retry_after_s == 5


@pytest.mark.asyncio
async def test_transition_alert_failure_does_not_block_authorize() -> None:
    class AlertBoom(FakeFacts):
        async def record_transition(self, **values: object) -> None:
            raise RuntimeError("alert_log unavailable")

    guard = SendAdmissionGuard(AlertBoom(facts(outbox_active=2000)))
    with pytest.raises(SendAdmissionRejected) as error:
        await guard.authorize(category="verify", channel="api", recipient_count=1)
    assert error.value.reason == "outbox_backlog"
    assert error.value.retry_after_s == 60


class FakeResult:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def mappings(self) -> FakeResult:
        return self

    def one(self) -> dict[str, object]:
        return self.row


class FakeConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return FakeResult(self.row)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)


class FakeRedis:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.keys: list[tuple[str, ...]] = []

    async def mget(self, *keys: str) -> list[object]:
        self.keys.append(keys)
        return list(self.values)


@pytest.mark.asyncio
async def test_repository_loads_same_keys_as_current_alerts() -> None:
    connection = FakeConnection(
        {
            "outbox_active": 12,
            "outbox_oldest_age_s": 45.2,
            "outbox_dead": 1,
            "uncertain_overdue": 2,
            "callback_dead": 3,
            "heartbeat_stale": 0,
        }
    )
    redis = FakeRedis(["999", None, "4"])
    repository = SqlSendAdmissionRepository(
        settings=type("S", (), {"database_url": "postgresql+asyncpg://x", "redis_control_url": "redis://x"})(),
        redis=redis,
    )
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    loaded = await repository.load()

    assert loaded == facts(
        outbox_active=12,
        outbox_oldest_age_s=45,
        outbox_dead=1,
        uncertain_overdue=2,
        callback_dead=3,
        realtime_paused=True,
        vendor_failures=4,
    )
    sql = connection.calls[0][0].casefold()
    assert "outbox_event" in sql and "created_at" in sql
    assert "pending" in sql and "dead" in sql
    assert "interval '24 hours'" in sql
    assert "callback_task" in sql
    assert "send_runtime_heartbeat" in sql
    assert "send-realtime" in sql
    assert "outbox-dispatcher" in sql
    for forbidden in ("phone_enc", "phone_hmac", "phone_mask", "secret", "broker"):
        assert forbidden not in sql
    assert redis.keys == [
        ("queue:paused:realtime", "queue:paused:bulk", "alert:vendor:consecutive_failures")
    ]


@pytest.mark.asyncio
async def test_repository_only_fail_closes_stale_heartbeats_in_production() -> None:
    row = {
        "outbox_active": 0,
        "outbox_oldest_age_s": 0,
        "outbox_dead": 0,
        "uncertain_overdue": 0,
        "callback_dead": 0,
        "heartbeat_stale": 1,
    }

    async def load_with(*, production: bool) -> SendAdmissionFacts:
        connection = FakeConnection(row)
        repository = SqlSendAdmissionRepository(
            settings=type(
                "S",
                (),
                {
                    "database_url": "postgresql+asyncpg://x",
                    "redis_control_url": "redis://x",
                    "is_production": production,
                },
            )(),
            redis=FakeRedis([None, None, "0"]),
        )
        repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
        return await repository.load()

    assert (await load_with(production=False)).heartbeat_stale is False
    assert (await load_with(production=True)).heartbeat_stale is True


@pytest.mark.asyncio
async def test_repository_fail_closes_on_invalid_vendor_counter() -> None:
    connection = FakeConnection(
        {
            "outbox_active": 0,
            "outbox_oldest_age_s": 0,
            "outbox_dead": 0,
            "uncertain_overdue": 0,
            "callback_dead": 0,
            "heartbeat_stale": 0,
        }
    )
    repository = SqlSendAdmissionRepository(
        settings=type("S", (), {"database_url": "postgresql+asyncpg://x", "redis_control_url": "redis://x"})(),
        redis=FakeRedis([None, None, "bad"]),
    )
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="vendor failure counter is invalid"):
        await repository.load()


@pytest.mark.asyncio
async def test_repository_records_transition_without_pii() -> None:
    connection = FakeConnection({})
    repository = SqlSendAdmissionRepository(
        settings=type("S", (), {"database_url": "postgresql+asyncpg://x", "redis_control_url": "redis://x"})(),
        redis=FakeRedis([]),
    )
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    await repository.record_transition(
        previous="open",
        state="closed",
        reason="outbox_backlog",
        version=3,
        facts=facts(outbox_active=2000),
    )
    sql, params = connection.calls[0]
    assert "alert_log" in sql.casefold()
    assert params["dedup_key"] == "send_admission:closed:outbox_backlog"
    assert params["level"] == "crit"
    assert "phone" not in params["detail"]
    assert "138" not in params["detail"]
    assert "outbox_active" in params["detail"]


@pytest.mark.asyncio
async def test_guard_uses_persisted_state_and_fails_closed_on_persist_error() -> None:
    from datetime import UTC, datetime, timedelta

    class PersistingFacts(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saved: list[dict[str, object]] = []
            self.fail = False

        async def load_control_state(self) -> dict[str, object]:
            return {
                "state": "closed",
                "reason_code": "outbox_backlog",
                "state_epoch": 4,
                "hold_until": datetime.now(UTC) + timedelta(seconds=30),
                "valid_until": datetime.now(UTC) + timedelta(seconds=10),
            }

        async def save_control_state(self, **values: object) -> None:
            if self.fail:
                raise RuntimeError("persist failed")
            self.saved.append(values)

    repo = PersistingFacts()
    guard = SendAdmissionGuard(repo)
    snap = await guard.snapshot()
    assert snap.state in {"closed", "degraded"}
    assert repo.saved
    repo.fail = True
    guard._snapshot = None
    with pytest.raises(SendAdmissionUnavailable):
        await guard.snapshot()


def test_send_worker_heartbeat_components_follow_queue_flags() -> None:
    from app.services.runtime_heartbeat import send_worker_heartbeat_components

    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "realtime")
    ) == ("send-realtime",)
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "bulk")
    ) == ("send-bulk",)
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "callback")
    ) == ()
    assert send_worker_heartbeat_components(("pytest",)) == (
        "send-realtime",
        "send-bulk",
    )
