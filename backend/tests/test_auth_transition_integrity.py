from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.observability import auth_observability_snapshot, reset_auth_observability
from app.core.auth.security_events import (
    AuthSecurityTransition,
    AuthTransitionDeadLetter,
    transition_dead_letter_hmac,
)
from app.core.auth.service import (
    AUDIT_DUE_KEY,
    AUDIT_RECOVERY_TTL_S,
    WRITER_LEASE_MS,
    AccountLocked,
    LoginGuard,
    RateLimited,
)
from app.core.auth.transition_sync import AuthTransitionReconciler
from tests.test_auth import FakeKeyValue, RecordingSecurityEvents

_GUARD_ERRORS = (AccountLocked, RateLimited, SessionStateUnavailable)


class RecordingAlerter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def emit_orphan(self, *, reason: str, field_class: str) -> None:
        self.events.append((reason, field_class))


class FailOnce(RecordingSecurityEvents):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
        self.calls += 1
        if self.calls == 1:
            raise SessionStateUnavailable("auth security audit unavailable")
        await super().ensure_transition(transition)


def _transition_id(store: FakeKeyValue, key: str) -> str:
    return str(store.values[key])


async def _lock_once(store: FakeKeyValue, writer: RecordingSecurityEvents, ip: str) -> str:
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", ip, "local")
    with suppress(*_GUARD_ERRORS):
        await guard.record_failure("user01", ip, "local")
    return _transition_id(store, "auth:lock:user:user01")


async def _pending_lock(store: FakeKeyValue, writer: FailOnce, ip: str) -> str:
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", ip, "local")
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", ip, "local")
    return _transition_id(store, "auth:lock:user:user01")


def _reconciler(
    store: FakeKeyValue,
    writer: RecordingSecurityEvents,
    alerter: RecordingAlerter | None = None,
) -> AuthTransitionReconciler:
    return AuthTransitionReconciler(
        store=store,
        security_events=writer,
        alerter=alerter or RecordingAlerter(),
        interval_s=1,
    )


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_auth_observability()


@pytest.mark.asyncio
async def test_transition_create_writes_envelope_state_and_due_atomically() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    lock = await _lock_once(store, writer, "10.0.1.1")
    audit = store.values[store._audit_key(lock)]
    assert audit["schema_version"] == "1"
    assert audit["transition_id"] == lock
    assert audit["action"] == "auth_account_locked"
    assert audit["provider_code"] == "local"
    assert audit["result_code"] == "ACCOUNT_LOCKED"
    assert int(audit["count"]) == 5
    assert int(audit["remaining_ttl_seconds"]) == 900
    assert audit["ip"] == "10.0.1.1"
    assert audit["object_kind"] == "account"
    assert audit["created_at_ms"] == 0
    assert lock not in store._due()
    assert lock not in store._open()
    assert writer.transitions[0].action == "auth_account_locked"


@pytest.mark.asyncio
async def test_transition_immutable_fields_cannot_be_overwritten() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    lock = await _lock_once(store, writer, "10.0.1.2")
    created = store.values[store._audit_key(lock)]["created_at_ms"]
    store.values["__now_ms"] = WRITER_LEASE_MS + 5
    with pytest.raises(_GUARD_ERRORS):
        await LoginGuard(store, security_events=writer).record_failure(
            "user01", "10.0.9.9", "ad"
        )
    audit = store.values[store._audit_key(lock)]
    assert audit["created_at_ms"] == created
    assert audit["provider_code"] == "local"
    assert audit["ip"] == "10.0.1.2"
    assert audit["action"] == "auth_account_locked"


@pytest.mark.asyncio
async def test_non_terminal_transition_payload_does_not_expire_before_settlement() -> None:
    store = FakeKeyValue()
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.3")
    assert store.values[store._audit_key(lock)]["state"] == "pending"
    assert store.ttl(store._audit_key(lock)) == -1


@pytest.mark.asyncio
async def test_due_member_without_hash_is_not_recreated() -> None:
    store = FakeKeyValue()
    orphan_id = str(uuid4())
    store._due()[orphan_id] = 0
    writer = RecordingSecurityEvents()
    await _reconciler(store, writer).reconcile()
    assert store._audit_key(orphan_id) not in store.values
    assert orphan_id not in store._due()
    assert writer.transitions == []


@pytest.mark.asyncio
async def test_due_member_without_hash_never_writes_default_audit() -> None:
    store = FakeKeyValue()
    orphan_id = str(uuid4())
    store._due()[orphan_id] = 0
    writer = RecordingSecurityEvents()
    alerter = RecordingAlerter()
    await _reconciler(store, writer, alerter).reconcile()
    assert writer.transitions == []
    assert alerter.events
    assert writer.dead_letters
    assert all(
        getattr(item, "action", None) != "auth_account_locked" for item in writer.transitions
    )


@pytest.mark.asyncio
async def test_incomplete_envelope_is_moved_to_dead_letter() -> None:
    store = FakeKeyValue()
    tid = str(uuid4())
    store.values[store._audit_key(tid)] = {"state": "pending", "action": "auth_ip_banned"}
    store._due()[tid] = 0
    writer = RecordingSecurityEvents()
    await _reconciler(store, writer).reconcile()
    assert writer.transitions == []
    assert tid not in store._due()
    record = writer.dead_letters[0]
    assert isinstance(record, AuthTransitionDeadLetter)
    assert record.reason == "incomplete_envelope"
    assert record.transition_hmac == transition_dead_letter_hmac(tid)
    assert store.values[store._audit_key(tid)]["state"] == "orphaned"


@pytest.mark.asyncio
async def test_ip_ban_orphan_never_becomes_account_lock() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    guard = LoginGuard(store, security_events=writer)
    for index in range(20):
        with suppress(*_GUARD_ERRORS):
            await guard.record_failure(f"ip-user-{index}", "10.0.1.4", "ad")
    ban = _transition_id(store, "auth:ban:ip:10.0.1.4")
    store.values.pop(store._audit_key(ban))
    store._due()[ban] = 0
    later = RecordingSecurityEvents()
    alerter = RecordingAlerter()
    await _reconciler(store, later, alerter).reconcile()
    assert later.transitions == []
    assert all(item.action != "auth_account_locked" for item in later.transitions)
    assert alerter.events[0][0] == "missing_hash"


@pytest.mark.asyncio
async def test_missing_provider_ip_count_or_result_fails_closed() -> None:
    store = FakeKeyValue()
    tid = str(uuid4())
    store.values[store._audit_key(tid)] = {
        "transition_id": tid,
        "schema_version": "1",
        "action": "auth_account_locked",
        "provider_code": "",
        "result_code": "ACCOUNT_LOCKED",
        "count": "5",
        "remaining_ttl_seconds": "900",
        "ip": "10.0.1.5",
        "created_at_ms": 0,
        "state": "pending",
    }
    store._due()[tid] = 0
    writer = RecordingSecurityEvents()
    await _reconciler(store, writer).reconcile()
    assert writer.transitions == []
    snapshot = auth_observability_snapshot()
    assert any(value > 0 for _reason, value in snapshot.transition_orphan)


@pytest.mark.asyncio
async def test_hash_without_due_is_repaired_from_original_schedule() -> None:
    store = FakeKeyValue()
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.6")
    original_score = store.values[store._audit_key(lock)]["next_retry_at_ms"]
    created = store.values[store._audit_key(lock)]["created_at_ms"]
    store._due().pop(lock, None)
    store.values["__now_ms"] = 2_000
    await _reconciler(store, writer).reconcile()
    assert store.values[store._audit_key(lock)]["created_at_ms"] == created
    assert store.values[store._audit_key(lock)]["action"] == "auth_account_locked"
    assert writer.transitions[-1].ip == "10.0.1.6"
    assert store.values[store._audit_key(lock)]["state"] == "audited"
    assert original_score == 1000


@pytest.mark.asyncio
async def test_ack_removes_due_and_applies_terminal_retention_atomically() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    lock = await _lock_once(store, writer, "10.0.1.7")
    key = store._audit_key(lock)
    assert store.values[key]["state"] == "audited"
    assert lock not in store._due()
    assert store.ttl(key) == AUDIT_RECOVERY_TTL_S


@pytest.mark.asyncio
async def test_fail_requeues_due_without_resetting_created_at() -> None:
    store = FakeKeyValue()
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.8")
    audit = store.values[store._audit_key(lock)]
    assert audit["created_at_ms"] == 0
    assert audit["state"] == "pending"
    assert lock in store._due()
    assert store.ttl(store._audit_key(lock)) == -1


@pytest.mark.asyncio
async def test_dead_removes_due_and_preserves_original_envelope() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    lock = await _lock_once(store, writer, "10.0.1.9")
    key = store._audit_key(lock)
    current = dict(store.values[key])
    current.update(
        {
            "state": "writing",
            "lease_id": "dead-lease",
            "attempts": 19,
            "created_at_ms": 0,
        }
    )
    store.values[key] = current
    store._due()[lock] = 0
    store.values["__now_ms"] = 10

    class FailAlways(RecordingSecurityEvents):
        async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
            raise SessionStateUnavailable("auth security audit unavailable")

    with pytest.raises(SessionStateUnavailable):
        await LoginGuard(store, security_events=FailAlways()).record_failure(
            "user01", "10.0.1.9", "local"
        )
    audit = store.values[key]
    assert audit["state"] == "dead"
    assert audit["action"] == "auth_account_locked"
    assert audit["ip"] == "10.0.1.9"
    assert audit["provider_code"] == "local"
    assert lock not in store._due()


@pytest.mark.asyncio
async def test_reconcile_after_more_than_24_hours_preserves_original_event() -> None:
    store = FakeKeyValue()
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.10")
    store.values["__now_ms"] = AUDIT_RECOVERY_TTL_S * 1000 + 5_000
    await _reconciler(store, writer).reconcile()
    assert writer.transitions[-1].action == "auth_account_locked"
    assert writer.transitions[-1].provider_code == "local"
    assert writer.transitions[-1].ip == "10.0.1.10"
    assert store.values[store._audit_key(lock)]["created_at_ms"] == 0
    assert store.values[store._audit_key(lock)]["state"] == "audited"


@pytest.mark.asyncio
async def test_redis_partial_restore_due_only() -> None:
    store = FakeKeyValue()
    tid = str(uuid4())
    store._due()[tid] = 0
    writer = RecordingSecurityEvents()
    await _reconciler(store, writer).reconcile()
    assert writer.transitions == []
    assert f"auth:audit:dead-letter:{tid}" in store.values
    assert tid not in store._due()


@pytest.mark.asyncio
async def test_redis_partial_restore_hash_only() -> None:
    store = FakeKeyValue()
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.11")
    store.values.pop(AUDIT_DUE_KEY, None)
    store.values["__now_ms"] = 2_000
    await _reconciler(store, writer).reconcile()
    assert store.values[store._audit_key(lock)]["action"] == "auth_account_locked"
    assert writer.transitions[-1].ip == "10.0.1.11"
    assert store.values[store._audit_key(lock)]["created_at_ms"] == 0


@pytest.mark.asyncio
async def test_redis_failover_and_snapshot_skew_integrity_contract() -> None:
    store = FakeKeyValue()
    due_only = str(uuid4())
    store._due()[due_only] = 0
    writer = FailOnce()
    lock = await _pending_lock(store, writer, "10.0.1.12")
    store._due().pop(lock, None)
    store.values["__now_ms"] = 2_000
    await _reconciler(store, writer).reconcile()
    assert writer.transitions[-1].action == "auth_account_locked"
    assert writer.transitions[-1].ip == "10.0.1.12"
    assert due_only not in store._due()
    assert all(item.ip != "0.0.0.0" for item in writer.transitions)
    assert all(item.action != "auth_ip_banned" for item in writer.transitions)


@pytest.mark.asyncio
async def test_eviction_or_manual_hash_delete_generates_alert_not_fake_audit() -> None:
    store = FakeKeyValue()
    writer = RecordingSecurityEvents()
    lock = await _lock_once(store, writer, "10.0.1.13")
    store.values.pop(store._audit_key(lock))
    store._due()[lock] = 0
    later = RecordingSecurityEvents()
    alerter = RecordingAlerter()
    await _reconciler(store, later, alerter).reconcile()
    assert later.transitions == []
    assert alerter.events
    snapshot = auth_observability_snapshot()
    assert any(value > 0 for _reason, value in snapshot.transition_dead_letter)


def test_writer_lease_budget_still_required() -> None:
    from app.core.auth.transition_sync import require_writer_lease_budget

    settings = SimpleNamespace(
        db_pool_timeout_seconds=3.0,
        db_connect_timeout_seconds=3.0,
        db_api_statement_timeout_ms=15_000,
    )
    require_writer_lease_budget(settings)


def test_dead_letter_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError):
        AuthTransitionDeadLetter(
            transition_hmac=transition_dead_letter_hmac(str(uuid4())),
            reason="missing_hash",
            field_class="missing_hash",
            discovered_at=datetime.now(),
            build_version="test",
        )


def test_dead_letter_hmac_is_stable_and_not_raw_uuid() -> None:
    tid = "8a5a77a4-286f-4d81-9a64-5379e30df986"
    digest = transition_dead_letter_hmac(tid)
    assert digest != tid
    assert len(digest) == 64
    assert transition_dead_letter_hmac(tid) == digest
    assert UUID(tid)


@pytest.mark.asyncio
@pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in __import__("os").environ,
    reason="requires isolated Redis 7",
)
async def test_real_redis_due_only_orphan_never_writes_lock_audit() -> None:
    import os

    from redis.asyncio import Redis

    from app.core.auth.service import AUDIT_DUE_KEY, AUDIT_OPEN_KEY, RedisKeyValue

    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    store = RedisKeyValue(client)
    orphan_id = str(uuid4())
    writer = RecordingSecurityEvents()
    alerter = RecordingAlerter()
    try:
        await client.zadd(AUDIT_DUE_KEY, {orphan_id: 0})
        await _reconciler(store, writer, alerter).reconcile()
        assert writer.transitions == []
        assert alerter.events
        assert writer.dead_letters
        assert await client.exists(f"auth:audit:transition:{orphan_id}") == 0
        assert await client.zscore(AUDIT_DUE_KEY, orphan_id) is None
    finally:
        await client.zrem(AUDIT_DUE_KEY, orphan_id)
        await client.zrem(AUDIT_OPEN_KEY, orphan_id)
        await client.delete(f"auth:audit:dead-letter:{orphan_id}")
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in __import__("os").environ,
    reason="requires isolated Redis 7",
)
async def test_real_redis_pending_hash_persists_until_ack() -> None:
    import os

    from redis.asyncio import Redis

    from app.core.auth.service import AUDIT_DUE_KEY, AUDIT_OPEN_KEY, RedisKeyValue

    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    store = RedisKeyValue(client)
    writer = FailOnce()
    username = f"ttl-{uuid4().hex[:12]}"
    ip = "10.9.4.8"
    lock = None
    try:
        guard = LoginGuard(store, security_events=writer)
        for _ in range(4):
            await guard.record_failure(username, ip, "local")
        with pytest.raises(SessionStateUnavailable):
            await guard.record_failure(username, ip, "local")
        lock = await client.get(f"auth:lock:user:{username}")
        key = f"auth:audit:transition:{lock}"
        envelope = await client.hgetall(key)
        assert envelope["action"] == "auth_account_locked"
        assert envelope["state"] == "pending"
        assert int(await client.ttl(key)) == -1
        assert await client.zscore(AUDIT_DUE_KEY, str(lock)) is not None
    finally:
        keys = [
            f"auth:fail:user:{username}",
            f"auth:lock:user:{username}",
            f"auth:fail:ip:{ip}",
        ]
        if lock:
            keys.append(f"auth:audit:transition:{lock}")
            await client.zrem(AUDIT_DUE_KEY, str(lock))
            await client.zrem(AUDIT_OPEN_KEY, str(lock))
        await client.delete(*keys)
        await client.aclose()
