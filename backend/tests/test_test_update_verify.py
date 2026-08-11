from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from test_update_store import TestUpdateState as State  # noqa: E402
from test_update_verify import TestUpdateVerify as UpdateVerify  # noqa: E402
from test_update_verify import TestUpdateVerifyError as VerifyError  # noqa: E402


class FakeStore:
    def __init__(self, state: State = State.APPLIED) -> None:
        self.state = state

    def transition(self, expected: State, target: State, **_: object) -> None:
        assert self.state is expected
        self.state = target

    def block(self, expected: State, **_: object) -> None:
        assert self.state is expected
        self.state = State.BLOCKED


class FakeVerifyOperations:
    def __init__(self, fail_at: str | None = None, *, mode: str = "live") -> None:
        self.events: list[object] = []
        self.fail_at = fail_at
        self.mode = mode

    def _event(self, value: object) -> None:
        self.events.append(value)
        if self.fail_at == value:
            raise RuntimeError("injected")

    def require_lifecycle_lock(self) -> None:
        self._event("lock")

    def verify_web(self) -> None:
        self._event("web")

    def validate_vendor_update_mode(self) -> str:
        self._event(("mode", self.mode))
        return self.mode

    def verify_budget_conservation(self) -> None:
        self._event("budget")

    def verify_pause_state(self) -> None:
        self._event("pauses")

    def probe_balance(self) -> None:
        self._event("get_balance")

    def recover_backend_services(self) -> None:
        self._event("recover_services")

    def verify_backend_services(self) -> None:
        self._event("services")

    def restore_owned_update_pauses(self, update_id: str) -> None:
        self._event(("restore_owned_pauses", update_id))

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        self._event(("rollback", kind, update_id))
        return "f" * 40, "0015"

    def cleanup_rollback_images(self, update_id: str) -> None:
        self._event(("cleanup_rollback", update_id))

    def hold_fail_closed(self, update_id: str) -> None:
        self.events.append(("hold", update_id))


def test_backend_verify_checks_all_live_invariants_before_owned_pause_restore() -> None:
    store = FakeStore()
    operations = FakeVerifyOperations()

    UpdateVerify(store, operations).verify(
        "backend-safe",
        update_id="test-api",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
    )

    assert operations.events == [
        "lock",
        ("mode", "live"),
        "budget",
        "pauses",
        "get_balance",
        "services",
        ("restore_owned_pauses", "test-api"),
        ("cleanup_rollback", "test-api"),
    ]
    assert store.state is State.VERIFIED


def test_recovered_backend_verify_uses_verifying_state_and_rechecks_all_invariants() -> None:
    store = FakeStore(State.VERIFYING)
    operations = FakeVerifyOperations()

    UpdateVerify(store, operations).verify(
        "backend-safe",
        update_id="test-recovery",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
        expected_state=State.VERIFYING,
    )

    assert operations.events == [
        "lock",
        ("mode", "live"),
        "budget",
        "pauses",
        "get_balance",
        "recover_services",
        "services",
        ("restore_owned_pauses", "test-recovery"),
        ("cleanup_rollback", "test-recovery"),
    ]
    assert store.state is State.VERIFIED


def test_pre_live_backend_verify_never_calls_real_vendor_balance() -> None:
    store = FakeStore()
    operations = FakeVerifyOperations(mode="pre-live")

    UpdateVerify(store, operations).verify(
        "backend-safe",
        update_id="test-pre-live",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
    )

    assert operations.events == [
        "lock",
        ("mode", "pre-live"),
        "budget",
        "pauses",
        "services",
        ("restore_owned_pauses", "test-pre-live"),
        ("cleanup_rollback", "test-pre-live"),
    ]
    assert "get_balance" not in operations.events
    assert store.state is State.VERIFIED


def test_web_verify_never_calls_vendor_or_restores_lanes() -> None:
    store = FakeStore()
    operations = FakeVerifyOperations()

    UpdateVerify(store, operations).verify(
        "web-only",
        update_id="test-web",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert operations.events == ["lock", "web", ("cleanup_rollback", "test-web")]
    assert store.state is State.VERIFIED


def test_backend_verify_failure_remains_paused_and_blocked() -> None:
    store = FakeStore()
    operations = FakeVerifyOperations(fail_at="get_balance")

    with pytest.raises(VerifyError, match="blocked"):
        UpdateVerify(store, operations).verify(
            "backend-safe",
            update_id="test-api",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
        )

    assert ("restore_owned_pauses", "test-api") not in operations.events
    assert operations.events[-1] == ("hold", "test-api")
    assert store.state is State.BLOCKED


def test_recovered_verify_failure_returns_to_blocked_without_pause_release() -> None:
    store = FakeStore(State.VERIFYING)
    operations = FakeVerifyOperations(fail_at="get_balance")

    with pytest.raises(VerifyError, match="blocked"):
        UpdateVerify(store, operations).verify(
            "backend-safe",
            update_id="test-recovery",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
            expected_state=State.VERIFYING,
        )

    assert ("restore_owned_pauses", "test-recovery") not in operations.events
    assert operations.events[-1] == ("hold", "test-recovery")
    assert store.state is State.BLOCKED


def test_recovered_verify_service_restart_failure_returns_to_blocked() -> None:
    store = FakeStore(State.VERIFYING)
    operations = FakeVerifyOperations(fail_at="recover_services")

    with pytest.raises(VerifyError, match="blocked"):
        UpdateVerify(store, operations).verify(
            "backend-safe",
            update_id="test-recovery",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
            expected_state=State.VERIFYING,
        )

    assert operations.events[:6] == [
        "lock",
        ("mode", "live"),
        "budget",
        "pauses",
        "get_balance",
        "recover_services",
    ]
    assert ("restore_owned_pauses", "test-recovery") not in operations.events
    assert operations.events[-1] == ("hold", "test-recovery")
    assert store.state is State.BLOCKED


def test_no_migration_verify_failure_rolls_back_previous_image() -> None:
    store = FakeStore()
    operations = FakeVerifyOperations(fail_at="services")

    with pytest.raises(VerifyError, match="rolled back"):
        UpdateVerify(store, operations).verify(
            "backend-safe",
            update_id="test-api",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0015",
        )

    assert operations.events[-1] == ("rollback", "backend-safe", "test-api")
    assert store.state is State.ROLLED_BACK
