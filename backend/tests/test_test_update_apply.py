from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from test_update_apply import BACKEND_SERVICES  # noqa: E402
from test_update_apply import TestUpdateApply as UpdateApply  # noqa: E402
from test_update_apply import TestUpdateApplyError as ApplyError  # noqa: E402
from test_update_store import TestUpdateState as State  # noqa: E402


class FakeStore:
    def __init__(self, state: State) -> None:
        self.state = state

    def transition(self, expected: State, target: State, **_: object) -> None:
        assert self.state is expected
        self.state = target

    def block(self, expected: State, **_: object) -> None:
        assert self.state is expected
        self.state = State.BLOCKED


class FakeApplyOperations:
    def __init__(self, fail_at: object | None = None, *, mode: str = "live") -> None:
        self.events: list[object] = []
        self.fail_at = fail_at
        self.mode = mode

    def _event(self, value: object) -> None:
        self.events.append(value)
        if self.fail_at == value:
            raise RuntimeError("injected")

    def require_lifecycle_lock(self) -> None:
        self._event("lock")

    def validate_vendor_update_mode(self) -> str:
        self._event(("mode", self.mode))
        return self.mode

    def require_owned_update_pauses(self, update_id: str) -> None:
        self._event(("pauses", update_id))

    def run_expand_migration(self, source: str, target: str) -> str:
        self._event(("migrate", source, target))
        return target

    def replace_backend_services(self, services: tuple[str, ...]) -> None:
        self._event(("replace_backend", services))

    def replace_web(self) -> None:
        self._event("replace_web")

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        self._event(("rollback", kind, update_id))
        return "f" * 40, "0015"

    def hold_fail_closed(self, update_id: str) -> None:
        self.events.append(("hold", update_id))


def test_backend_apply_migrates_then_replaces_fixed_services_without_mock() -> None:
    store = FakeStore(State.CHECKPOINTED)
    operations = FakeApplyOperations()

    UpdateApply(store, operations).apply(
        "backend-safe",
        update_id="test-api",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
    )

    assert "mock-vendor" not in BACKEND_SERVICES
    assert operations.events == [
        "lock",
        ("mode", "live"),
        ("pauses", "test-api"),
        ("migrate", "0015", "0016"),
        ("replace_backend", BACKEND_SERVICES),
    ]
    assert store.state is State.APPLIED


def test_web_apply_only_replaces_web() -> None:
    store = FakeStore(State.PREPARED)
    operations = FakeApplyOperations()

    UpdateApply(store, operations).apply(
        "web-only",
        update_id="test-web",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert operations.events == ["lock", "replace_web"]
    assert store.state is State.APPLIED


def test_backend_without_migration_skips_migration_and_checkpoint_state() -> None:
    store = FakeStore(State.PREPARED)
    operations = FakeApplyOperations()

    UpdateApply(store, operations).apply(
        "backend-safe",
        update_id="test-api",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert operations.events == [
        "lock",
        ("mode", "live"),
        ("pauses", "test-api"),
        ("replace_backend", BACKEND_SERVICES),
    ]
    assert store.state is State.APPLIED


def test_backend_apply_failure_blocks_without_mock_fallback_or_restore() -> None:
    store = FakeStore(State.CHECKPOINTED)
    operations = FakeApplyOperations(fail_at="not-used")

    def fail_replace(_services: tuple[str, ...]) -> None:
        operations.events.append("replace_failed")
        raise RuntimeError("injected")

    operations.replace_backend_services = fail_replace  # type: ignore[method-assign]
    with pytest.raises(ApplyError, match="blocked"):
        UpdateApply(store, operations).apply(
            "backend-safe",
            update_id="test-api",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
        )

    assert store.state is State.BLOCKED
    assert operations.events[-1] == ("hold", "test-api")
    assert not any("mock" in str(event) or "restore" in str(event) for event in operations.events)


def test_backend_no_migration_apply_failure_restores_previous_image() -> None:
    store = FakeStore(State.PREPARED)
    operations = FakeApplyOperations()

    def fail_replace(_services: tuple[str, ...]) -> None:
        operations.events.append("replace_failed")
        raise RuntimeError("injected")

    operations.replace_backend_services = fail_replace  # type: ignore[method-assign]
    with pytest.raises(ApplyError, match="rolled back"):
        UpdateApply(store, operations).apply(
            "backend-safe",
            update_id="test-api",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0015",
        )

    assert store.state is State.ROLLED_BACK
    assert operations.events[-1] == ("rollback", "backend-safe", "test-api")
    assert not any(event == ("hold", "test-api") for event in operations.events)


def test_backend_apply_revalidates_pre_live_mode_before_migration() -> None:
    store = FakeStore(State.CHECKPOINTED)
    operations = FakeApplyOperations(mode="pre-live")

    UpdateApply(store, operations).apply(
        "backend-safe",
        update_id="test-pre-live",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
    )

    assert operations.events[:3] == [
        "lock",
        ("mode", "pre-live"),
        ("pauses", "test-pre-live"),
    ]
    assert store.state is State.APPLIED


@pytest.mark.parametrize("replacement", ["missing", "predecessor", "manual", "daily"])
def test_backend_apply_blocks_before_migration_when_pause_ownership_changed(
    replacement: str,
) -> None:
    store = FakeStore(State.CHECKPOINTED)
    operations = FakeApplyOperations(fail_at=("pauses", "test-api"))
    operations.events.append(("observed_replacement", replacement))

    with pytest.raises(ApplyError, match="blocked at pauses"):
        UpdateApply(store, operations).apply(
            "backend-safe",
            update_id="test-api",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
        )

    assert not any(
        isinstance(event, tuple) and event[0] == "migrate"
        for event in operations.events
    )
    assert operations.events[-1] == ("hold", "test-api")
