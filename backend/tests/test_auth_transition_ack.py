from __future__ import annotations

from contextlib import suppress
from uuid import UUID

import pytest

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.security_events import AuthSecurityTransition
from app.core.auth.service import AccountLocked, LoginGuard, RateLimited
from tests.test_auth import FakeKeyValue, RecordingSecurityEvents

_GUARD_ERRORS = (AccountLocked, RateLimited, SessionStateUnavailable)


class CountingWriter(RecordingSecurityEvents):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.fail_times = 0

    async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SessionStateUnavailable("auth security audit unavailable")
        await super().ensure_transition(transition)


def _lock_ttl(store: FakeKeyValue, username: str = "user01") -> int | None:
    key = f"auth:lock:user:{username}"
    return 900 if key in store.values else None


@pytest.mark.asyncio
async def test_lock_transition_writer_called_once_after_ack() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    guard = LoginGuard(store, security_events=writer)
    for _ in range(5):
        with suppress(*_GUARD_ERRORS):
            await guard.record_failure("user01", "10.0.0.21", "local")
    assert writer.calls == 1
    for _ in range(3):
        with pytest.raises(_GUARD_ERRORS):
            await guard.record_failure("user01", "10.0.0.21", "local")
    assert writer.calls == 1


@pytest.mark.asyncio
async def test_ban_transition_writer_called_once_after_ack() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    guard = LoginGuard(store, security_events=writer)
    for index in range(20):
        with suppress(*_GUARD_ERRORS):
            await guard.record_failure(f"ip-user-{index}", "10.0.0.22", "local")
    ban_calls = [item for item in writer.transitions if item.action == "auth_ip_banned"]
    assert len(ban_calls) == 1
    first = writer.calls
    with pytest.raises(RateLimited):
        await guard.admit("local", "other", "10.0.0.22")
    assert writer.calls == first


@pytest.mark.asyncio
async def test_audited_transition_rejection_performs_zero_database_writes() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    guard = LoginGuard(store, security_events=writer)
    for _ in range(5):
        with suppress(*_GUARD_ERRORS):
            await guard.record_failure("user01", "10.0.0.23", "local")
    assert writer.calls == 1
    writer.calls = 0
    with pytest.raises(_GUARD_ERRORS):
        await guard.record_failure("user01", "10.0.0.23", "local")
    assert writer.calls == 0


@pytest.mark.asyncio
async def test_pending_transition_before_retry_at_performs_zero_database_writes() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    writer.fail_times = 1
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", "10.0.0.24", "local")
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.24", "local")
    assert writer.calls == 1
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.24", "local")
    assert writer.calls == 1


@pytest.mark.asyncio
async def test_writer_failure_sets_bounded_retry_backoff() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    writer.fail_times = 1
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", "10.0.0.25", "local")
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.25", "local")
    audit = next(value for key, value in store.values.items() if str(key).startswith("auth:audit:"))
    assert audit["state"] == "pending"
    assert audit["attempts"] == 1
    assert int(audit["next_retry_at_ms"]) == 1000


@pytest.mark.asyncio
async def test_only_one_concurrent_request_claims_writer_lease() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    guard = LoginGuard(store, security_events=writer)
    for _ in range(5):
        with suppress(*_GUARD_ERRORS):
            await guard.record_failure("user01", "10.0.0.26", "local")
    lock = str(store.values["auth:lock:user:user01"])
    claimed, state = store._claim_write(lock, "other-lease")
    assert claimed == ""
    assert state == "audited"


@pytest.mark.asyncio
async def test_expired_writer_lease_can_be_fenced_and_recovered() -> None:
    store = FakeKeyValue()
    store.values["__now_ms"] = 0
    writer = CountingWriter()
    writer.fail_times = 100
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", "10.0.0.27", "local")
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.27", "local")
    lock = str(store.values["auth:lock:user:user01"])
    store.values[store._audit_key(lock)] = {
        "state": "writing",
        "lease_id": "dead-lease",
        "lease_expires_ms": 1,
        "attempts": 1,
    }
    store.values["__now_ms"] = 10
    writer.fail_times = 0
    with pytest.raises(_GUARD_ERRORS):
        await guard.record_failure("user01", "10.0.0.27", "local")
    assert writer.calls >= 2
    assert store.values[store._audit_key(lock)]["state"] == "audited"


@pytest.mark.asyncio
async def test_stale_writer_ack_cannot_mark_new_lease_audited() -> None:
    store = FakeKeyValue()
    store.values["auth:audit:transition:8a5a77a4-286f-4d81-9a64-5379e30df986"] = {
        "state": "writing",
        "lease_id": "new-lease",
        "attempts": 1,
    }
    result = await store.eval(
        "-- auth-audit-ack-v1\n",
        1,
        "auth:audit:transition:8a5a77a4-286f-4d81-9a64-5379e30df986",
        "old-lease",
        "900",
    )
    assert result == 0
    assert (
        store.values["auth:audit:transition:8a5a77a4-286f-4d81-9a64-5379e30df986"]["state"]
        == "writing"
    )


@pytest.mark.asyncio
async def test_database_insert_committed_but_ack_lost_is_deduplicated_and_acked() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    guard = LoginGuard(store, security_events=writer)
    original_eval = store.eval

    async def drop_first_ack(script: str, numkeys: int, *args: object) -> object:
        if "auth-audit-ack-v1" in script and not getattr(drop_first_ack, "done", False):
            drop_first_ack.done = True  # type: ignore[attr-defined]
            return 0
        return await original_eval(script, numkeys, *args)

    store.eval = drop_first_ack  # type: ignore[method-assign]
    for _ in range(4):
        await guard.record_failure("user01", "10.0.0.28", "local")
    with pytest.raises(_GUARD_ERRORS):
        await guard.record_failure("user01", "10.0.0.28", "local")
    assert writer.calls == 1
    store.values["__now_ms"] = 10_000
    with pytest.raises(_GUARD_ERRORS):
        await guard.record_failure("user01", "10.0.0.28", "local")
    assert writer.calls == 2
    lock = str(store.values["auth:lock:user:user01"])
    assert store.values[store._audit_key(lock)]["state"] == "audited"
    assert str(UUID(lock)) == lock


@pytest.mark.asyncio
async def test_transition_audit_retry_does_not_extend_lock_or_ban_ttl() -> None:
    store = FakeKeyValue()
    writer = CountingWriter()
    writer.fail_times = 1
    guard = LoginGuard(store, security_events=writer)
    for _ in range(4):
        await guard.record_failure("user01", "10.0.0.29", "local")
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.29", "local")
    lock_before = store.values["auth:lock:user:user01"]
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure("user01", "10.0.0.29", "local")
    assert store.values["auth:lock:user:user01"] == lock_before
    assert _lock_ttl(store) == 900
