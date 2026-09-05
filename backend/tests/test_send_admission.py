from __future__ import annotations

from typing import Any

import pytest

from app.services.send_admission import (
    SendAdmissionFacts,
    SendAdmissionGuard,
    SendAdmissionRejected,
    SendAdmissionSnapshot,
    SendAdmissionUnavailable,
    authorize_from_snapshot,
    decide,
    evaluate_capacity,
    transition_admission_state,
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
    assert evaluate_capacity(facts(dispatcher_heartbeat_stale=True)) == (
        "closed",
        "dispatcher_heartbeat_stale",
    )
    assert evaluate_capacity(facts(realtime_heartbeat_stale=True)) == ("open", "ok")
    assert evaluate_capacity(facts(bulk_heartbeat_stale=True)) == ("open", "ok")
    assert evaluate_capacity(
        facts(realtime_heartbeat_stale=True, bulk_heartbeat_stale=True)
    ) == ("closed", "send_lanes_heartbeat_stale")


def test_closed_recovery_creates_hold_and_returns_degraded() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 5, 16, 0, tzinfo=UTC)
    state, reason, hold = transition_admission_state(
        previous="closed",
        raw_state="open",
        raw_reason="ok",
        db_now=now,
        hold_until=None,
    )
    assert (state, reason) == ("degraded", "recovery_hold")
    assert hold == now + timedelta(seconds=60)

    kept, kept_reason, still = transition_admission_state(
        previous="degraded",
        raw_state="open",
        raw_reason="ok",
        db_now=now,
        hold_until=now + timedelta(seconds=30),
    )
    assert (kept, kept_reason, still) == (
        "degraded",
        "recovery_hold",
        now + timedelta(seconds=30),
    )

    opened, opened_reason, cleared = transition_admission_state(
        previous="degraded",
        raw_state="open",
        raw_reason="ok",
        db_now=now,
        hold_until=now - timedelta(seconds=1),
    )
    assert (opened, opened_reason, cleared) == ("open", "ok", None)

    closed, closed_reason, closed_hold = transition_admission_state(
        previous="degraded",
        raw_state="closed",
        raw_reason="outbox_backlog",
        db_now=now,
        hold_until=now + timedelta(seconds=30),
    )
    assert (closed, closed_reason, closed_hold) == ("closed", "outbox_backlog", None)

    leftover, leftover_reason, leftover_hold = transition_admission_state(
        previous="open",
        raw_state="open",
        raw_reason="ok",
        db_now=now,
        hold_until=now + timedelta(seconds=40),
    )
    assert (leftover, leftover_reason) == ("degraded", "recovery_hold")
    assert leftover_hold == now + timedelta(seconds=40)

    bootstrapped, boot_reason, boot_hold = transition_admission_state(
        previous=None,
        raw_state="open",
        raw_reason="ok",
        db_now=now,
        hold_until=None,
    )
    assert (bootstrapped, boot_reason) == ("degraded", "recovery_hold")
    assert boot_hold == now + timedelta(seconds=60)

    degraded_raw, degraded_reason, degraded_hold = transition_admission_state(
        previous="closed",
        raw_state="degraded",
        raw_reason="vendor_failures",
        db_now=now,
        hold_until=None,
    )
    assert (degraded_raw, degraded_reason) == ("degraded", "recovery_hold")
    assert degraded_hold == now + timedelta(seconds=60)


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


def test_recovery_hold_allows_large_realtime_but_not_market() -> None:
    recovering = facts(outbox_active=120)
    large = decide(
        recovering,
        category="verify",
        recipient_count=500,
        previous_state="closed",
    )
    assert large.allowed is True
    assert large.state == "degraded"
    assert large.reason == "recovery_hold"
    market = decide(
        recovering,
        category="market",
        recipient_count=1,
        previous_state="closed",
    )
    assert market.allowed is False
    assert market.reason == "degraded_bulk"
    real_degraded = decide(facts(outbox_active=200), category="verify", recipient_count=500)
    assert real_degraded.allowed is False
    assert real_degraded.reason == "degraded_volume"


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


def test_lane_heartbeat_closes_only_that_category() -> None:
    bulk_only = facts(bulk_heartbeat_stale=True)
    assert evaluate_capacity(bulk_only) == ("open", "ok")
    notice = decide(bulk_only, category="notice", recipient_count=1)
    assert notice.allowed is True
    assert notice.reason == "ok"
    market = decide(bulk_only, category="market", recipient_count=1)
    assert market.allowed is False
    assert market.reason == "bulk_heartbeat_stale"
    realtime_only = facts(realtime_heartbeat_stale=True)
    assert decide(realtime_only, category="verify", recipient_count=1).reason == (
        "realtime_heartbeat_stale"
    )
    assert decide(realtime_only, category="market", recipient_count=1).allowed is True
    dispatcher = facts(dispatcher_heartbeat_stale=True)
    assert decide(dispatcher, category="notice", recipient_count=1).reason == (
        "dispatcher_heartbeat_stale"
    )
    assert decide(dispatcher, category="market", recipient_count=1).reason == (
        "dispatcher_heartbeat_stale"
    )
    both_lanes = facts(realtime_heartbeat_stale=True, bulk_heartbeat_stale=True)
    assert decide(both_lanes, category="notice", recipient_count=1).reason == (
        "send_lanes_heartbeat_stale"
    )


def test_snapshot_lane_heartbeat_does_not_close_the_other_lane() -> None:
    snap = SendAdmissionSnapshot(1, 0.0, facts(bulk_heartbeat_stale=True), "open", "ok")
    notice = authorize_from_snapshot(snap, category="notice", recipient_count=1)
    assert notice.allowed is True
    market = authorize_from_snapshot(snap, category="market", recipient_count=1)
    assert market.allowed is False
    assert market.reason == "bulk_heartbeat_stale"
    hold = SendAdmissionSnapshot(
        1,
        0.0,
        facts(realtime_heartbeat_stale=True),
        "degraded",
        "recovery_hold",
    )
    verify = authorize_from_snapshot(hold, category="verify", recipient_count=1)
    assert verify.allowed is False
    assert verify.reason == "realtime_heartbeat_stale"
    market_hold = authorize_from_snapshot(hold, category="market", recipient_count=1)
    assert market_hold.allowed is False
    assert market_hold.reason == "degraded_bulk"


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
            "realtime_heartbeat_stale": 0,
            "bulk_heartbeat_stale": 0,
            "dispatcher_heartbeat_stale": 0,
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
    assert "send-bulk" in sql
    assert "outbox-dispatcher" in sql
    assert "required(component)" not in sql
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
        "realtime_heartbeat_stale": 0,
        "bulk_heartbeat_stale": 1,
        "dispatcher_heartbeat_stale": 0,
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

    development = await load_with(production=False)
    assert development.bulk_heartbeat_stale is False
    assert development.realtime_heartbeat_stale is False
    production = await load_with(production=True)
    assert production.bulk_heartbeat_stale is True
    assert production.realtime_heartbeat_stale is False
    assert production.dispatcher_heartbeat_stale is False


@pytest.mark.asyncio
async def test_repository_fail_closes_on_invalid_vendor_counter() -> None:
    connection = FakeConnection(
        {
            "outbox_active": 0,
            "outbox_oldest_age_s": 0,
            "outbox_dead": 0,
            "uncertain_overdue": 0,
            "callback_dead": 0,
            "realtime_heartbeat_stale": 0,
            "bulk_heartbeat_stale": 0,
            "dispatcher_heartbeat_stale": 0,
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
async def test_first_closed_recovery_saves_degraded_hold_not_open() -> None:
    from datetime import UTC, datetime, timedelta

    class PersistingFacts(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saved: list[dict[str, object]] = []

        async def load_control_state(self) -> dict[str, object]:
            now = datetime.now(UTC)
            return {
                "state": "closed",
                "reason_code": "outbox_backlog",
                "state_epoch": 1,
                "hold_until": None,
                "valid_until": now + timedelta(seconds=10),
                "db_now": now,
            }

        async def save_control_state(self, **values: object) -> None:
            self.saved.append(values)

    repo = PersistingFacts()
    guard = SendAdmissionGuard(repo)
    snap = await guard.snapshot()
    assert snap.state == "degraded"
    assert snap.reason == "recovery_hold"
    assert repo.saved[0]["state"] == "degraded"
    assert repo.saved[0]["reason"] == "recovery_hold"
    assert repo.saved[0]["hold_until"] is not None


@pytest.mark.asyncio
async def test_open_plus_future_hold_is_reread_as_recovery_hold() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    class LeftoverOpenHold(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saved: list[dict[str, object]] = []

        async def load_control_state(self) -> dict[str, object]:
            return {
                "state": "open",
                "reason_code": "ok",
                "state_epoch": 2,
                "hold_until": now + timedelta(seconds=45),
                "valid_until": now + timedelta(seconds=10),
                "db_now": now,
            }

        async def save_control_state(self, **values: object) -> None:
            self.saved.append(values)

    repo = LeftoverOpenHold()
    snap = await SendAdmissionGuard(repo).snapshot()
    assert snap.state == "degraded"
    assert snap.reason == "recovery_hold"
    assert repo.saved[0]["state"] == "degraded"
    assert repo.saved[0]["reason"] == "recovery_hold"


@pytest.mark.asyncio
async def test_bootstrap_and_expired_state_enter_recovery_hold() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    class MissingRow(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saved: list[dict[str, object]] = []

        async def load_control_state(self) -> None:
            return None

        async def save_control_state(self, **values: object) -> None:
            self.saved.append(values)

    missing = MissingRow()
    snap = await SendAdmissionGuard(missing).snapshot()
    assert snap.state == "degraded"
    assert snap.reason == "recovery_hold"
    assert missing.saved[0]["hold_until"] is not None

    class ExpiredRow(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saved: list[dict[str, object]] = []

        async def load_control_state(self) -> dict[str, object]:
            return {
                "state": "open",
                "reason_code": "ok",
                "state_epoch": 5,
                "hold_until": None,
                "valid_until": now - timedelta(seconds=1),
                "db_now": now,
            }

        async def save_control_state(self, **values: object) -> None:
            self.saved.append(values)

    expired = ExpiredRow()
    expired_snap = await SendAdmissionGuard(expired).snapshot()
    assert expired_snap.state == "degraded"
    assert expired_snap.reason == "recovery_hold"


@pytest.mark.asyncio
async def test_repository_rejects_open_with_hold() -> None:
    from datetime import UTC, datetime, timedelta

    repository = SqlSendAdmissionRepository(
        settings=type(
            "S",
            (),
            {"database_url": "postgresql+asyncpg://x", "redis_control_url": "redis://x"},
        )(),
        redis=FakeRedis([]),
    )
    with pytest.raises(ValueError, match="open admission state cannot carry"):
        await repository.save_control_state(
            state="open",
            reason="ok",
            hold_until=datetime.now(UTC) + timedelta(seconds=60),
            epoch=1,
        )
    with pytest.raises(ValueError, match="recovery_hold must be a degraded"):
        await repository.save_control_state(
            state="closed",
            reason="recovery_hold",
            hold_until=None,
            epoch=1,
        )


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

    empty = {"ENVIRONMENT": "test"}
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "realtime"),
        environ=empty,
    ) == ("send-realtime",)
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "bulk"),
        environ=empty,
    ) == ("send-bulk",)
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "callback"),
        environ=empty,
    ) == ()
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "realtime-report"),
        environ=empty,
    ) == ()
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "--queues=realtime,callback"),
        environ=empty,
    ) == ("send-realtime",)
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "--queues", "bulk-report"),
        environ=empty,
    ) == ()
    assert send_worker_heartbeat_components(("pytest",), environ=empty) == (
        "send-realtime",
        "send-bulk",
    )
    assert send_worker_heartbeat_components(
        ("pytest",),
        environ={"ENVIRONMENT": "production"},
    ) == ()
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "realtime-report"),
        environ={"SMS_RUNTIME_HEARTBEAT_COMPONENTS": "none"},
    ) == ()
    assert send_worker_heartbeat_components(
        ("celery", "-A", "app.tasks", "worker", "-Q", "realtime-report"),
        environ={"SMS_RUNTIME_HEARTBEAT_COMPONENTS": "send-realtime"},
    ) == ("send-realtime",)


@pytest.mark.asyncio
async def test_authorize_keeps_recovery_hold_from_snapshot() -> None:
    healthy = facts()
    snap = SendAdmissionSnapshot(1, 0.0, healthy, "degraded", "recovery_hold")
    notice = authorize_from_snapshot(snap, category="notice", recipient_count=20)
    assert notice.allowed is True
    assert notice.state == "degraded"
    assert notice.reason == "recovery_hold"
    oversized = authorize_from_snapshot(snap, category="notice", recipient_count=500)
    assert oversized.allowed is True
    assert oversized.reason == "recovery_hold"
    market = authorize_from_snapshot(snap, category="market", recipient_count=1)
    assert market.allowed is False
    assert market.reason == "degraded_bulk"

    class RecoveryFacts:
        async def load(self) -> SendAdmissionFacts:
            return healthy

        async def load_control_state(self) -> dict[str, object]:
            from datetime import UTC, datetime, timedelta

            return {
                "state": "closed",
                "reason_code": "outbox_backlog",
                "state_epoch": 3,
                "hold_until": datetime.now(UTC) + timedelta(seconds=60),
                "valid_until": datetime.now(UTC) + timedelta(seconds=30),
            }

        async def save_control_state(self, **values: object) -> None:
            self.saved = values

    guard = SendAdmissionGuard(RecoveryFacts())
    with pytest.raises(SendAdmissionRejected) as rejected:
        await guard.authorize(category="market", channel="api", recipient_count=1)
    assert rejected.value.state == "degraded"
    assert rejected.value.reason == "degraded_bulk"


@pytest.mark.asyncio
async def test_cas_loser_adopts_fresh_equivalent_winner() -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.send_admission_repository import AdmissionControlConflict

    now = datetime.now(UTC)
    winner = {
        "state": "open",
        "reason_code": "ok",
        "state_epoch": 8,
        "hold_until": None,
        "valid_until": now + timedelta(seconds=10),
        "db_now": now,
        "outcome": "adopted",
    }

    class RacingFacts(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts())
            self.saves = 0

        async def load_control_state(self) -> dict[str, object]:
            return {
                "state": "open",
                "reason_code": "ok",
                "state_epoch": 7,
                "hold_until": None,
                "valid_until": now + timedelta(seconds=10),
                "db_now": now,
            }

        async def save_control_state(self, **values: object) -> dict[str, object]:
            self.saves += 1
            raise AdmissionControlConflict(winner)

    guard = SendAdmissionGuard(RacingFacts())
    snap = await guard.snapshot()
    assert snap.state == "open"
    assert snap.reason == "ok"


@pytest.mark.asyncio
async def test_cas_loser_retries_when_local_candidate_is_stricter() -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.send_admission_repository import AdmissionControlConflict

    now = datetime.now(UTC)
    open_winner = {
        "state": "open",
        "reason_code": "ok",
        "state_epoch": 8,
        "hold_until": None,
        "valid_until": now + timedelta(seconds=10),
        "db_now": now,
        "outcome": "adopted",
    }

    class ClosedThenAdopt(FakeFacts):
        def __init__(self) -> None:
            super().__init__(facts(dispatcher_heartbeat_stale=True))
            self.saves = 0

        async def load_control_state(self) -> dict[str, object]:
            if self.saves == 0:
                return {
                    "state": "open",
                    "reason_code": "ok",
                    "state_epoch": 7,
                    "hold_until": None,
                    "valid_until": now + timedelta(seconds=10),
                    "db_now": now,
                }
            return {
                "state": "closed",
                "reason_code": "dispatcher_heartbeat_stale",
                "state_epoch": 9,
                "hold_until": None,
                "valid_until": now + timedelta(seconds=10),
                "db_now": now,
            }

        async def save_control_state(self, **values: object) -> dict[str, object]:
            self.saves += 1
            if self.saves == 1:
                raise AdmissionControlConflict(open_winner)
            return {
                "state": values["state"],
                "reason_code": values["reason"],
                "state_epoch": 9,
                "outcome": "saved",
            }

    repo = ClosedThenAdopt()
    guard = SendAdmissionGuard(repo)
    snap = await guard.snapshot()
    assert snap.state == "closed"
    assert snap.reason == "dispatcher_heartbeat_stale"
    assert repo.saves == 2
