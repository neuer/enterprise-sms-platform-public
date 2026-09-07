from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace
from typing import Any
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
    INTEGRITY_STATS_PAGE_MAX,
    WRITER_LEASE_MS,
    AccountLocked,
    LoginGuard,
    RateLimited,
)
from app.core.auth.transition_sync import AuthTransitionReconciler
from tests.test_auth import FakeKeyValue, RecordingSecurityEvents

_GUARD_ERRORS = (AccountLocked, RateLimited, SessionStateUnavailable)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_busy_open_prefix_cannot_starve_hash_work(
    monkeypatch: pytest.MonkeyPatch, pending: bool,
) -> None:
    store = ScriptedHashScanStore({})
    writer = RecordingSecurityEvents()
    prefix = [_put_hash_only(store, ip=f"10.0.6.{i}") for i in (1, 2)]
    for tid in prefix:
        store._open()[tid] = 0
        store._due()[tid] = 10**15
        store.values[_audit_key(tid)]["next_retry_at_ms"] = 10**15
    target = _put_hash_only(store, ip="10.0.6.3", created_at_ms=7777)
    store.pages = _scan_pages((_audit_key(target),), ())
    reconciler = _reconciler(store, writer)
    if pending:
        reconciler._pending_scan_batch.append(_audit_key(target))
    clock = [0.0]
    reconciler._clock = lambda: clock[0]
    original = LoginGuard.repair_transition_integrity

    async def costly(guard: LoginGuard, transition_id: str) -> Any:
        if transition_id in prefix:
            clock[0] += 0.6
        return await original(guard, transition_id)

    monkeypatch.setattr(LoginGuard, "repair_transition_integrity", costly)
    await reconciler.reconcile()
    assert store.values[_audit_key(target)]["state"] == "pending"
    await reconciler.reconcile()
    assert store.values[_audit_key(target)]["state"] == "audited"
    assert len(writer.transitions) == 1
    assert store.values[_audit_key(target)]["created_at_ms"] == 7777
    await reconciler.reconcile()
    assert len(writer.transitions) == 1


@pytest.mark.asyncio
async def test_cancelled_hash_item_is_kept_until_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ScriptedHashScanStore({})
    target = _put_hash_only(store, ip="10.0.6.4")
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    reconciler._pending_scan_batch.append(_audit_key(target))
    entered = asyncio.Event()
    original = LoginGuard.repair_transition_integrity

    async def blocked(_guard: LoginGuard, _transition_id: str) -> Any:
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(LoginGuard, "repair_transition_integrity", blocked)
    task = asyncio.create_task(reconciler.reconcile())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(reconciler._pending_scan_batch) == [_audit_key(target)]
    monkeypatch.setattr(LoginGuard, "repair_transition_integrity", original)
    await reconciler.reconcile()
    assert store.values[_audit_key(target)]["state"] == "audited"
    assert len(writer.transitions) == 1


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


def _audit_key(transition_id: str) -> str:
    return f"auth:audit:transition:{transition_id}"


def _put_hash_only(
    store: FakeKeyValue,
    *,
    ip: str,
    created_at_ms: int = 4242,
    action: str = "auth_account_locked",
    provider_code: str = "ad",
    transition_id: str | None = None,
) -> str:
    tid = transition_id or str(uuid4())
    result = "ACCOUNT_LOCKED" if action == "auth_account_locked" else "RATE_LIMITED"
    store.values[store._audit_key(tid)] = {
        "transition_id": tid,
        "schema_version": "1",
        "action": action,
        "provider_code": provider_code,
        "result_code": result,
        "count": "5",
        "remaining_ttl_seconds": "900",
        "ip": ip,
        "created_at_ms": created_at_ms,
        "state": "pending",
        "next_retry_at_ms": created_at_ms + 1000,
        "object_kind": "account" if action == "auth_account_locked" else "ip",
    }
    store.values["__now_ms"] = max(int(store.values.get("__now_ms", 0)), created_at_ms + 2000)
    return tid


def _scan_pages(*batches: tuple[str, ...]) -> dict[str, tuple[str, tuple[str, ...]]]:
    pages: dict[str, tuple[str, tuple[str, ...]]] = {}
    current = "0"
    for index, keys in enumerate(batches, start=1):
        nxt = "0" if index == len(batches) else f"c{index}"
        pages[current] = (nxt, keys)
        current = nxt
    return pages


def _long_scan_pages(
    *,
    target_key: str | None = None,
    extra: int = 1,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    batches: list[tuple[str, ...]] = [() for _ in range(8)]
    batches.append((target_key,) if target_key else ())
    batches.extend(() for _ in range(extra))
    return _scan_pages(*batches)


def _assert_cursor_walk(
    store: ScriptedHashScanStore,
    cursors: list[str],
) -> None:
    current = "0"
    for cursor in cursors:
        assert cursor == current
        current, _keys = store.pages[cursor]


class ScriptedHashScanStore(FakeKeyValue):
    """按预设不透明游标序列返回 SCAN 页，覆盖超过 8 次调用的尾部。"""

    def __init__(self, pages: dict[str, tuple[str, tuple[str, ...]]]) -> None:
        super().__init__()
        self.pages = pages
        self.scan_cursors: list[str] = []
        self.scan_calls = 0
        self.scan_in_flight = 0
        self.max_scan_in_flight = 0
        self.fail_on_call: int | None = None
        self.hold_on_call: int | None = None
        self.hold_gate = asyncio.Event()
        self.scan_started = asyncio.Event()
        self.scan_delay_s = 0.0
        self.storage_identity = "gen-1"
        self.stats_calls: list[tuple[str, int, int]] = []

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        if "auth-audit-integrity-scan-v1" in script:
            return await self._scan(str(args[0]))
        if "auth-audit-integrity-stats-page-v1" in script:
            self.stats_calls.append((str(args[0]), int(args[1]), int(args[2])))
            assert int(args[2]) <= INTEGRITY_STATS_PAGE_MAX
        if "auth-audit-integrity-stats-v1" in script:
            raise AssertionError("unbounded integrity stats script must not run")
        return await super().eval(script, numkeys, *args)

    async def _scan(self, cursor: str) -> list[Any]:
        self.scan_calls += 1
        self.scan_cursors.append(cursor)
        self.scan_in_flight += 1
        self.max_scan_in_flight = max(self.max_scan_in_flight, self.scan_in_flight)
        try:
            if self.scan_delay_s:
                await asyncio.sleep(self.scan_delay_s)
            if self.hold_on_call == self.scan_calls:
                self.scan_started.set()
                await self.hold_gate.wait()
            if self.fail_on_call == self.scan_calls:
                raise SessionStateUnavailable("hash scan failed")
            if cursor not in self.pages:
                raise SessionStateUnavailable("unknown scan cursor")
            next_cursor, keys = self.pages[cursor]
            return [next_cursor, list(keys)]
        finally:
            self.scan_in_flight -= 1


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
async def test_real_redis_wrong_type_does_not_block_pending_hash_tail() -> None:
    import os

    from redis.asyncio import Redis

    from app.core.auth.service import AUDIT_OPEN_KEY, RedisKeyValue

    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    fixture = FakeKeyValue()
    healthy = _put_hash_only(fixture, ip="10.0.6.8")
    poison = str(uuid4())
    writer = RecordingSecurityEvents()
    try:
        await client.set(_audit_key(poison), "synthetic-wrong-type")
        await client.hset(_audit_key(healthy), mapping=fixture.values[_audit_key(healthy)])
        reconciler = _reconciler(RedisKeyValue(client), writer)
        reconciler._pending_scan_batch.extend([_audit_key(poison), _audit_key(healthy)])
        await reconciler.reconcile()
        assert not reconciler._pending_scan_batch
        assert await client.hget(_audit_key(healthy), "state") == "audited"
        assert len(writer.transitions) == 1
        assert writer.dead_letters
        assert await client.type(_audit_key(poison)) == "string"
    finally:
        await client.zrem(AUDIT_DUE_KEY, poison, healthy)
        await client.zrem(AUDIT_OPEN_KEY, poison, healthy)
        await client.delete(_audit_key(poison), _audit_key(healthy),
                            f"auth:audit:dead-letter:{poison}")
        await client.aclose()


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


@pytest.mark.asyncio
async def test_integrity_scan_preserves_cursor_across_ticks() -> None:
    store = ScriptedHashScanStore(_long_scan_pages())
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    await reconciler.reconcile()
    assert store.scan_cursors == ["0", "c1", "c2", "c3", "c4", "c5", "c6", "c7"]
    _assert_cursor_walk(store, store.scan_cursors)
    assert reconciler._hash_scan_cursor == "c8"
    assert reconciler.scan_in_progress
    await reconciler.reconcile()
    assert store.scan_cursors[8:] == ["c8", "c9"]
    _assert_cursor_walk(store, store.scan_cursors)
    assert reconciler._hash_scan_cursor == "0"
    assert not reconciler.scan_in_progress


@pytest.mark.asyncio
async def test_hash_only_tail_beyond_eight_scan_calls_is_recovered() -> None:
    store = ScriptedHashScanStore({})
    tid = _put_hash_only(store, ip="10.0.8.9", created_at_ms=7777, provider_code="ad")
    store.pages = _long_scan_pages(target_key=_audit_key(tid))
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    await reconciler.reconcile()
    assert tid not in store._due()
    assert writer.transitions == []
    assert store.scan_calls == 8
    await reconciler.reconcile()
    assert store.values[store._audit_key(tid)]["created_at_ms"] == 7777
    assert store.values[store._audit_key(tid)]["action"] == "auth_account_locked"
    assert writer.transitions[-1].ip == "10.0.8.9"
    assert writer.transitions[-1].provider_code == "ad"
    assert store.values[store._audit_key(tid)]["state"] == "audited"


@pytest.mark.asyncio
async def test_empty_page_with_nonzero_cursor_does_not_end_cycle() -> None:
    store = ScriptedHashScanStore(_scan_pages((), (), ()))
    reconciler = _reconciler(store, RecordingSecurityEvents())
    reconciler.scan_calls_per_tick = 1
    await reconciler.reconcile()
    assert store.scan_cursors == ["0"]
    assert reconciler._hash_scan_cursor == "c1"
    assert reconciler.scan_in_progress
    assert auth_observability_snapshot().integrity_scan_cycles_completed == 0
    await reconciler.reconcile()
    assert store.scan_cursors == ["0", "c1"]
    assert reconciler._hash_scan_cursor == "c2"


@pytest.mark.asyncio
async def test_duplicate_scan_results_are_idempotent() -> None:
    store = ScriptedHashScanStore({})
    tid = _put_hash_only(store, ip="10.0.8.10", created_at_ms=8888)
    key = _audit_key(tid)
    store.pages = _scan_pages((key,), (key,), ())
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    await reconciler.reconcile()
    assert len(writer.transitions) == 1
    assert writer.transitions[0].ip == "10.0.8.10"
    assert store.values[store._audit_key(tid)]["created_at_ms"] == 8888
    await reconciler.reconcile()
    assert len(writer.transitions) == 1


@pytest.mark.asyncio
async def test_oversized_scan_batch_is_not_truncated_or_lost() -> None:
    store = ScriptedHashScanStore({})
    tids = [_put_hash_only(store, ip=f"10.0.8.{index}") for index in range(20, 25)]
    keys = tuple(_audit_key(tid) for tid in tids)
    store.pages = _scan_pages(keys, ())
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    reconciler.hash_process_budget = 2
    await reconciler.reconcile()
    pending = [key.rsplit(":", 1)[-1] for key in reconciler._pending_scan_batch]
    found = {item.transition_id for item in writer.transitions} | set(pending)
    assert found == set(tids)
    assert len(pending) == 3
    assert reconciler._hash_scan_cursor == "c1"
    while pending or reconciler._hash_scan_cursor != "0":
        await reconciler.reconcile()
        pending = [key.rsplit(":", 1)[-1] for key in reconciler._pending_scan_batch]
    assert {item.transition_id for item in writer.transitions} == set(tids)
    assert all(store.values[store._audit_key(tid)]["state"] == "audited" for tid in tids)


@pytest.mark.asyncio
async def test_scan_budget_does_not_reset_progress() -> None:
    store = ScriptedHashScanStore(_long_scan_pages())
    reconciler = _reconciler(store, RecordingSecurityEvents())
    await reconciler.reconcile()
    assert store.scan_calls == 8
    assert reconciler._hash_scan_cursor == "c8"
    first = list(store.scan_cursors)
    await reconciler.reconcile()
    assert store.scan_cursors[:8] == first
    assert store.scan_cursors[8] == "c8"
    assert reconciler._hash_scan_cursor == "0"


@pytest.mark.asyncio
async def test_concurrent_reconcile_does_not_overwrite_scan_cursor() -> None:
    store = ScriptedHashScanStore(_scan_pages(*[() for _ in range(20)]))
    store.scan_delay_s = 0.01
    reconciler = _reconciler(store, RecordingSecurityEvents())
    await asyncio.gather(reconciler.reconcile(), reconciler.reconcile())
    assert store.max_scan_in_flight == 1
    _assert_cursor_walk(store, store.scan_cursors)
    assert store.scan_calls == 16
    assert reconciler._hash_scan_cursor == "c16"


@pytest.mark.asyncio
async def test_scan_error_or_cancel_preserves_confirmed_progress() -> None:
    store = ScriptedHashScanStore(_long_scan_pages())
    reconciler = _reconciler(store, RecordingSecurityEvents())
    store.fail_on_call = 3
    await reconciler.reconcile()
    assert store.scan_cursors == ["0", "c1", "c2"]
    assert reconciler._hash_scan_cursor == "c2"
    store.fail_on_call = None
    store.hold_on_call = 4
    task = asyncio.create_task(reconciler.reconcile())
    await asyncio.wait_for(store.scan_started.wait(), timeout=1)
    assert reconciler._hash_scan_cursor == "c2"
    task.cancel()
    store.hold_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reconciler._hash_scan_cursor == "c2"


@pytest.mark.asyncio
async def test_storage_reset_restarts_one_full_scan() -> None:
    store = ScriptedHashScanStore(_long_scan_pages())
    reconciler = _reconciler(store, RecordingSecurityEvents())
    await reconciler.reconcile()
    assert reconciler._hash_scan_cursor == "c8"
    store.storage_identity = "gen-2"
    await reconciler.reconcile()
    assert store.scan_cursors[8] == "0"
    assert reconciler._hash_scan_cursor == "c8"
    _assert_cursor_walk(store, store.scan_cursors[8:])


@pytest.mark.asyncio
async def test_repair_preserves_original_envelope_and_created_at() -> None:
    store = ScriptedHashScanStore({})
    tid = _put_hash_only(
        store,
        ip="10.0.8.30",
        created_at_ms=9090,
        action="auth_ip_banned",
        provider_code="local",
    )
    store.pages = _long_scan_pages(target_key=_audit_key(tid))
    writer = RecordingSecurityEvents()
    reconciler = _reconciler(store, writer)
    await reconciler.reconcile()
    await reconciler.reconcile()
    audit = store.values[store._audit_key(tid)]
    assert audit["created_at_ms"] == 9090
    assert audit["action"] == "auth_ip_banned"
    assert audit["provider_code"] == "local"
    assert audit["ip"] == "10.0.8.30"
    assert audit["result_code"] == "RATE_LIMITED"
    assert writer.transitions[-1].action == "auth_ip_banned"
    assert writer.transitions[-1].provider_code == "local"
    assert writer.transitions[-1].ip == "10.0.8.30"


@pytest.mark.asyncio
async def test_integrity_statistics_uses_bounded_batches() -> None:
    store = ScriptedHashScanStore(_scan_pages(()))
    for index in range(20):
        tid = str(uuid4())
        store.values[store._audit_key(tid)] = {
            "state": "pending",
            "action": "auth_account_locked",
        }
        store._open()[tid] = index
    for _ in range(20):
        store._due()[str(uuid4())] = 99_999
    script = LoginGuard._INTEGRITY_STATS_PAGE_LUA
    assert "ZRANGE', OPEN, 0, -1" not in script
    assert "ZRANGE', DUE, 0, -1" not in script
    assert not hasattr(LoginGuard, "_INTEGRITY_STATS_LUA")
    reconciler = _reconciler(store, RecordingSecurityEvents())
    reconciler.stats_page_size = 8
    reconciler.stats_pages_per_tick = 1
    await reconciler.reconcile()
    assert store.stats_calls == [("open", 0, 8)]
    snapshot = auth_observability_snapshot()
    assert snapshot.integrity_stats_complete == 0
    assert snapshot.transition_pending_without_due == 0
    assert snapshot.transition_due_without_payload == 0
    for _ in range(5):
        await reconciler.reconcile()
    snapshot = auth_observability_snapshot()
    assert snapshot.integrity_stats_complete == 1
    assert snapshot.transition_pending_without_due == 20
    assert snapshot.transition_due_without_payload == 20
    assert all(limit <= INTEGRITY_STATS_PAGE_MAX for _index, _offset, limit in store.stats_calls)
    assert all(limit == 8 for _index, _offset, limit in store.stats_calls)


@pytest.mark.asyncio
async def test_full_cycle_metrics_update_only_after_cursor_zero() -> None:
    store = ScriptedHashScanStore(_long_scan_pages())
    reconciler = _reconciler(store, RecordingSecurityEvents())
    await reconciler.reconcile()
    snapshot = auth_observability_snapshot()
    assert snapshot.integrity_scan_cycles_completed == 0
    assert snapshot.integrity_scan_in_progress == 1
    assert snapshot.integrity_scan_processed > 0 or store.scan_calls == 8
    await reconciler.reconcile()
    snapshot = auth_observability_snapshot()
    assert snapshot.integrity_scan_cycles_completed == 1
    assert snapshot.integrity_scan_in_progress == 0
    assert reconciler._hash_scan_cursor == "0"
