from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import vendor_test_manager as manager_module  # noqa: E402


class FakeOperations:
    def __init__(
        self,
        *,
        counts: dict[str, int] | None = None,
        fail_at: str | None = None,
        active_recipients: int = 1,
        pause_kind: str | None = None,
    ) -> None:
        self.events: list[str] = []
        self.counts = counts or {status: 0 for status in manager_module.ACTIVE_STATUSES}
        self.fail_at = fail_at
        self.active_recipients = active_recipients
        self.pause_kind = pause_kind

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise RuntimeError("formal-key-private 13800138000 " + "a" * 64)

    def require_lifecycle_lock(self) -> None:
        self._event("lock")

    def pause_activation_lanes(self) -> None:
        self._event("pause")

    def current_pause_kind(self) -> str | None:
        self._event("pause_kind")
        return self.pause_kind

    def stop_senders(self) -> None:
        self._event("stop_senders")

    def active_status_counts(self) -> dict[str, int]:
        self._event("active_counts")
        return self.counts

    def create_encrypted_checkpoint(self) -> str:
        self._event("checkpoint")
        return "activation-20260716T010203Z"

    def active_recipient_count(self) -> int:
        self._event("active_recipients")
        return self.active_recipients

    def remove_mock_vendor(self) -> None:
        self._event("remove_mock")

    def validate_compose(self) -> None:
        self._event("compose_config")

    def start_core(self) -> None:
        self._event("start_core")

    def probe_balance(self) -> manager_module.BalanceProbe:
        self._event("get_balance")
        return manager_module.BalanceProbe(
            code=0,
            message="ok",
            balance=9988,
            observed_at="2026-07-16T01:02:03Z",
            correlation_id="activation-20260716T010203Z",
        )

    def budget_snapshot(self) -> manager_module.BudgetSnapshot:
        self._event("budget")
        return manager_module.BudgetSnapshot(0, 0, 0)

    def start_senders(self) -> None:
        self._event("start_senders")

    def clear_activation_pause(self) -> None:
        self._event("unpause_activation")

    def hold_fail_closed(self) -> None:
        self.events.append("hold_fail_closed")


@dataclass(frozen=True)
class FakeRotationTransaction:
    previous_generation: Path = Path("/private/previous-generation")
    new_generation: Path = Path("/private/new-generation")
    phase: str = "switched"


class FakeRotationStore:
    def __init__(self, transaction: FakeRotationTransaction | None = None) -> None:
        self.events: list[str] = []
        self.transaction = transaction

    def begin_rotation(self) -> FakeRotationTransaction:
        self.events.append("begin_rotation")
        self.transaction = FakeRotationTransaction()
        return self.transaction

    def commit_rotation(self, transaction: FakeRotationTransaction) -> None:
        assert transaction == self.transaction
        self.events.append("commit_rotation")
        self.transaction = None

    def rollback_to_previous(
        self,
        transaction: FakeRotationTransaction,
    ) -> FakeRotationTransaction:
        assert transaction == self.transaction
        self.events.append("rollback_to_previous")
        self.transaction = replace(transaction, phase="rollback_started")
        return self.transaction

    def complete_rollback(self, transaction: FakeRotationTransaction) -> None:
        assert transaction == self.transaction
        self.events.append("complete_rollback")
        self.transaction = None

    def read_rotation_transaction(self) -> FakeRotationTransaction | None:
        self.events.append("read_rotation_transaction")
        return self.transaction

    def discard_pending(self) -> None:
        self.events.append("discard_pending")

    def recover_pending(self) -> str:
        self.events.append("recover_pending")
        return "discarded"


def _paths(tmp_path: Path) -> manager_module.ActivationPaths:
    credentials = tmp_path / "secrets"
    credentials.mkdir(mode=0o700)
    for name in ("vendor_secret_name", "vendor_secret_key"):
        path = credentials / name
        path.write_text("not-inspected", encoding="utf-8")
        path.chmod(0o600)

    dotenv = tmp_path / ".env"
    dotenv.write_text("DEBUG=0\nCOMPOSE_PROFILES=dev\n", encoding="utf-8")
    dotenv.chmod(0o600)
    return manager_module.ActivationPaths(
        runtime_root=tmp_path / "runtime",
        credential_source=credentials,
        marker_file=tmp_path / "test-environment.json",
        dotenv_file=dotenv,
        evidence_file=tmp_path / "activation-evidence.json",
    )


def test_status_before_activation_reports_inactive_without_creating_marker(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)

    status = manager_module._safe_status(
        paths.marker_file,
        1,
        expected_uid=os.geteuid(),
    )

    assert status == {
        "schema_version": 1,
        "recipient_count": 1,
        "status": "inactive",
    }
    assert not paths.marker_file.exists()


def test_status_rejects_invalid_existing_marker(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.marker_file.write_text("{}\n", encoding="utf-8")
    paths.marker_file.chmod(0o600)

    with pytest.raises(manager_module.VendorTestFileError, match="marker fields"):
        manager_module._safe_status(
            paths.marker_file,
            1,
            expected_uid=os.geteuid(),
        )


def test_cli_status_before_activation_returns_inactive_without_vendor_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "missing-marker"
    state_dir = tmp_path / "vendor-test"
    state_dir.mkdir()
    root = tmp_path / "platform"
    root.mkdir()

    class StatusOperations:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def _psql(self, statement: str) -> str:
            assert statement == "SELECT version_num FROM alembic_version"
            return "202607170001"

        def active_recipient_count(self) -> int:
            return 1

        def current_pause_kind(self) -> None:
            raise AssertionError("inactive status must not inspect Redis pauses")

        def probe_balance(self) -> None:
            raise AssertionError("status must not call the vendor")

    monkeypatch.setattr(manager_module, "_require_cli_paths", lambda _args: None)
    monkeypatch.setattr(manager_module, "_private_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager_module, "HostActivationOperations", StatusOperations)

    result = manager_module.main(
        [
            "status",
            "--root",
            str(root),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--state-dir",
            str(state_dir),
            "--marker-file",
            str(marker),
            "--dotenv-file",
            str(root / ".env"),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "actual_migration_head": "202607170001",
        "recipient_count": 1,
        "pause_kind": None,
        "schema_version": 1,
        "status": "inactive",
    }
    assert not marker.exists()


def test_cli_rotate_before_first_activation_only_switches_pending_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "missing-marker"
    state_dir = tmp_path / "vendor-test"
    state_dir.mkdir()
    root = tmp_path / "platform"
    root.mkdir()
    events: list[str] = []

    class InactiveOperations:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def active_recipient_count(self) -> int:
            raise AssertionError("inactive credential replacement needs no recipient")

    class Store:
        def __init__(self, path: Path) -> None:
            assert path == state_dir / "credentials"

        def activate_pending(self):
            events.append("activate_pending")
            return object(), Path("/private/previous-generation")

    monkeypatch.setattr(manager_module, "_require_cli_paths", lambda _args: None)
    monkeypatch.setattr(manager_module, "_private_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager_module, "require_inherited_lifecycle_lock", lambda _path: None)
    monkeypatch.setattr(
        manager_module,
        "validate_installed_vendor_credentials",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(manager_module, "HostActivationOperations", InactiveOperations)
    monkeypatch.setattr(manager_module, "VendorCredentialStore", Store)
    monkeypatch.setattr(
        manager_module,
        "reconcile_pure_mock_dotenv",
        lambda path, **_kwargs: events.append(f"pure_mock:{path.name}"),
    )

    result = manager_module.main(
        [
            "rotate",
            "--root",
            str(root),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--state-dir",
            str(state_dir),
            "--marker-file",
            str(marker),
            "--dotenv-file",
            str(root / ".env"),
        ]
    )

    assert result == 0
    assert events == ["pure_mock:.env", "activate_pending"]
    assert json.loads(capsys.readouterr().out) == {"status": "rotated"}


def test_cli_rotate_without_marker_fails_closed_when_dotenv_is_not_pure_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "missing-marker"
    state_dir = tmp_path / "vendor-test"
    state_dir.mkdir()
    root = tmp_path / "platform"
    root.mkdir()
    dotenv = root / ".env"
    dotenv.write_text("VENDOR_MOCK=0\n", encoding="utf-8")
    events: list[str] = []

    class InactiveOperations:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def hold_fail_closed(self) -> None:
            events.append("hold_fail_closed")

        def active_recipient_count(self) -> int:
            raise AssertionError("unsafe inactive replacement must stop first")

    class Store:
        def __init__(self, _path: Path) -> None:
            pass

        def activate_pending(self) -> None:
            raise AssertionError("unsafe state must not switch credentials")

    def reject_live(path: Path, **_kwargs: object) -> None:
        assert path == dotenv
        raise manager_module.VendorTestFileError("pure Mock mode required")

    monkeypatch.setattr(manager_module, "_require_cli_paths", lambda _args: None)
    monkeypatch.setattr(manager_module, "_private_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager_module, "require_inherited_lifecycle_lock", lambda _path: None)
    monkeypatch.setattr(
        manager_module,
        "validate_installed_vendor_credentials",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(manager_module, "HostActivationOperations", InactiveOperations)
    monkeypatch.setattr(manager_module, "VendorCredentialStore", Store)
    monkeypatch.setattr(manager_module, "reconcile_pure_mock_dotenv", reject_live)

    result = manager_module.main(
        [
            "rotate",
            "--root",
            str(root),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--state-dir",
            str(state_dir),
            "--marker-file",
            str(marker),
            "--dotenv-file",
            str(dotenv),
        ]
    )

    assert result == 1
    assert events == ["hold_fail_closed"]


def test_activation_runs_the_exact_fail_closed_sequence(tmp_path: Path) -> None:
    operations = FakeOperations()
    paths = _paths(tmp_path)
    activation = manager_module.VendorTestActivationManager(
        operations,
        paths,
        expected_uid=os.geteuid(),
        clock=lambda: datetime(2026, 7, 16, 1, 2, 3, tzinfo=UTC),
    )

    result = activation.activate()

    assert result.status == "activated"
    assert operations.events == [
        "lock",
        "pause",
        "stop_senders",
        "active_counts",
        "checkpoint",
        "active_recipients",
        "remove_mock",
        "compose_config",
        "start_core",
        "get_balance",
        "budget",
        "start_senders",
        "unpause_activation",
    ]
    assert paths.marker_file.is_file()
    assert "VENDOR_MOCK=0" in paths.dotenv_file.read_text(encoding="utf-8")
    assert (
        "SMS_VENDOR_TEST_STATE_DIR=/var/lib/sms-platform/vendor-test"
        in paths.dotenv_file.read_text(encoding="utf-8")
    )
    evidence = json.loads(paths.evidence_file.read_text(encoding="utf-8"))
    assert evidence == {
        "balance": 9988,
        "checkpoint_id": "activation-20260716T010203Z",
        "code": 0,
        "correlation_id": "activation-20260716T010203Z",
        "message": "ok",
        "observed_at": "2026-07-16T01:02:03Z",
        "schema_version": 1,
        "status": "activated",
    }


def test_rotation_runs_pause_checkpoint_switch_rebuild_probe_and_unpause() -> None:
    class RotationOperations(FakeOperations):
        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

    operations = RotationOperations()
    store = FakeRotationStore()

    result = manager_module.VendorTestRotationManager(operations, store).rotate()

    assert result.status == "rotated"
    assert result.checkpoint_id == "activation-20260716T010203Z"
    assert operations.events == [
        "lock",
        "pause_kind",
        "pause",
        "stop_senders",
        "active_counts",
        "checkpoint",
        "prepare_runtime_secrets",
        "compose_config",
        "rebuild_backend",
        "get_balance",
        "unpause_activation",
    ]
    assert store.events == ["begin_rotation", "commit_rotation"]


def test_rotation_failure_restores_previous_runtime_and_keeps_critical_pause() -> None:
    class RotationOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__()
            self.probe_count = 0

        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

        def probe_balance(self) -> manager_module.BalanceProbe:
            self.probe_count += 1
            if self.probe_count == 1:
                self._event("get_balance")
                raise RuntimeError("formal-key-private 13800138000 " + "a" * 64)
            return super().probe_balance()

    operations = RotationOperations()
    store = FakeRotationStore()

    with pytest.raises(manager_module.VendorTestActivationError, match="rotation"):
        manager_module.VendorTestRotationManager(operations, store).rotate()

    assert store.events == [
        "begin_rotation",
        "rollback_to_previous",
        "complete_rollback",
    ]
    assert operations.events == [
        "lock",
        "pause_kind",
        "pause",
        "stop_senders",
        "active_counts",
        "checkpoint",
        "prepare_runtime_secrets",
        "compose_config",
        "rebuild_backend",
        "get_balance",
        "hold_fail_closed",
        "prepare_runtime_secrets",
        "compose_config",
        "rebuild_backend",
        "get_balance",
    ]


def test_rotation_failure_before_pointer_switch_discards_pending_candidate() -> None:
    class RotationOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__(fail_at="checkpoint")

        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

    operations = RotationOperations()
    store = FakeRotationStore()

    with pytest.raises(manager_module.VendorTestActivationError, match="rotation"):
        manager_module.VendorTestRotationManager(operations, store).rotate()

    assert store.events == ["discard_pending"]
    assert operations.events == [
        "lock",
        "pause_kind",
        "pause",
        "stop_senders",
        "active_counts",
        "checkpoint",
        "hold_fail_closed",
    ]


def test_rotation_rollback_failure_keeps_durable_transaction_for_recovery() -> None:
    class RotationOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__()
            self.rebuild_count = 0

        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self.rebuild_count += 1
            self._event("rebuild_backend")
            if self.rebuild_count == 2:
                raise RuntimeError("rollback runtime failed")

        def probe_balance(self) -> manager_module.BalanceProbe:
            self._event("get_balance")
            raise RuntimeError("new credential probe failed")

    operations = RotationOperations()
    store = FakeRotationStore()

    with pytest.raises(manager_module.VendorTestActivationError, match="rotation"):
        manager_module.VendorTestRotationManager(operations, store).rotate()

    assert store.transaction is not None
    assert store.transaction.phase == "rollback_started"
    assert "complete_rollback" not in store.events
    assert operations.events[-1] == "rebuild_backend"


def test_rotation_recovery_rebuilds_previous_runtime_and_probes_before_cleanup() -> None:
    class RecoveryOperations(FakeOperations):
        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

    transaction = FakeRotationTransaction(phase="switched")
    store = FakeRotationStore(transaction)
    operations = RecoveryOperations(pause_kind="manual")

    result = manager_module.VendorTestRotationRecoveryManager(
        operations,
        store,
    ).recover()

    assert result == "rolled_back"
    assert store.events == [
        "read_rotation_transaction",
        "rollback_to_previous",
        "complete_rollback",
    ]
    assert operations.events == [
        "lock",
        "hold_fail_closed",
        "stop_senders",
        "prepare_runtime_secrets",
        "compose_config",
        "rebuild_backend",
        "get_balance",
    ]


def test_rotation_recovery_only_discards_orphan_candidate_without_transaction() -> None:
    store = FakeRotationStore()
    operations = FakeOperations()

    result = manager_module.VendorTestRotationRecoveryManager(
        operations,
        store,
    ).recover()

    assert result == "discarded"
    assert store.events == ["read_rotation_transaction", "recover_pending"]
    assert operations.events == ["lock"]


def test_rotation_recovery_preserves_safe_vendor_error_code() -> None:
    class RejectedRecoveryOperations(FakeOperations):
        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

        def probe_balance(self) -> manager_module.BalanceProbe:
            self._event("get_balance")
            raise manager_module.VendorTestProbeRejected(1010)

    store = FakeRotationStore(FakeRotationTransaction())

    with pytest.raises(manager_module.VendorTestProbeRejected) as captured:
        manager_module.VendorTestRotationRecoveryManager(
            RejectedRecoveryOperations(),
            store,
        ).recover()

    assert captured.value.vendor_code == 1010
    assert store.transaction is not None
    assert store.transaction.phase == "rollback_started"


def test_rotation_preserves_existing_manual_pause_after_success() -> None:
    class RotationOperations(FakeOperations):
        def prepare_runtime_secrets(self) -> None:
            self._event("prepare_runtime_secrets")

        def rebuild_backend(self) -> None:
            self._event("rebuild_backend")

    operations = RotationOperations(pause_kind="manual")

    result = manager_module.VendorTestRotationManager(
        operations,
        FakeRotationStore(),
    ).rotate()

    assert result.status == "rotated"
    assert "pause" not in operations.events
    assert "unpause_activation" not in operations.events
    assert operations.events[:4] == [
        "lock",
        "pause_kind",
        "stop_senders",
        "active_counts",
    ]


def test_rotation_rejects_any_active_delivery_before_switching_credentials() -> None:
    class RotationOperations(FakeOperations):
        def prepare_runtime_secrets(self) -> None:
            raise AssertionError

        def rebuild_backend(self) -> None:
            raise AssertionError

    operations = RotationOperations(counts={"queued": 1})
    store = FakeRotationStore()

    with pytest.raises(manager_module.VendorTestActivationError, match="rotation"):
        manager_module.VendorTestRotationManager(operations, store).rotate()

    assert "active_counts" in operations.events
    assert store.events == ["discard_pending"]
    assert "begin_rotation" not in store.events
    assert "unpause_activation" not in operations.events


@pytest.mark.parametrize("status", manager_module.ACTIVE_STATUSES)
def test_any_active_status_blocks_before_checkpoint_and_holds_pause(
    tmp_path: Path,
    status: str,
) -> None:
    counts = {name: 0 for name in manager_module.ACTIVE_STATUSES}
    counts[status] = 1
    operations = FakeOperations(counts=counts)
    paths = _paths(tmp_path)
    activation = manager_module.VendorTestActivationManager(
        operations,
        paths,
        expected_uid=os.geteuid(),
    )

    with pytest.raises(manager_module.VendorTestActivationError, match="blocked"):
        activation.activate()

    assert "checkpoint" not in operations.events
    assert operations.events[-1] == "hold_fail_closed"
    evidence = json.loads(paths.evidence_file.read_text(encoding="utf-8"))
    assert evidence["status"] == "blocked"
    assert evidence["step"] == "active_counts"


@pytest.mark.parametrize("fail_at", ["checkpoint", "get_balance", "budget"])
def test_failure_never_starts_later_services_or_leaks_error_details(
    tmp_path: Path,
    fail_at: str,
) -> None:
    operations = FakeOperations(fail_at=fail_at)
    paths = _paths(tmp_path)
    activation = manager_module.VendorTestActivationManager(
        operations,
        paths,
        expected_uid=os.geteuid(),
    )

    with pytest.raises(manager_module.VendorTestActivationError) as captured:
        activation.activate()

    rendered = str(captured.value) + paths.evidence_file.read_text(encoding="utf-8")
    assert "formal-key-private" not in rendered
    assert "13800138000" not in rendered
    assert "a" * 64 not in rendered
    assert operations.events[-1] == "hold_fail_closed"
    if fail_at in {"checkpoint", "get_balance"}:
        assert "start_senders" not in operations.events


def test_budget_must_be_zero_before_workers_start(tmp_path: Path) -> None:
    class UsedBudgetOperations(FakeOperations):
        def budget_snapshot(self) -> manager_module.BudgetSnapshot:
            self._event("budget")
            return manager_module.BudgetSnapshot(1, 0, 0)

    operations = UsedBudgetOperations()
    activation = manager_module.VendorTestActivationManager(
        operations,
        _paths(tmp_path),
        expected_uid=os.geteuid(),
    )

    with pytest.raises(manager_module.VendorTestActivationError, match="budget"):
        activation.activate()

    assert "start_senders" not in operations.events
    assert operations.events[-1] == "hold_fail_closed"


def test_postgres_must_have_at_least_one_active_recipient(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    operations = FakeOperations(active_recipients=0)

    with pytest.raises(manager_module.VendorTestActivationError, match="recipients"):
        manager_module.VendorTestActivationManager(
            operations,
            paths,
            expected_uid=os.geteuid(),
        ).activate()

    assert "remove_mock" not in operations.events
    assert operations.events[-1] == "hold_fail_closed"


def test_host_counts_active_postgres_recipients_without_exporting_hmac() -> None:
    operations = object.__new__(manager_module.HostActivationOperations)
    statements: list[str] = []

    def psql(statement: str) -> str:
        statements.append(statement)
        return "2"

    operations._psql = psql  # type: ignore[method-assign]

    count = operations.active_recipient_count()

    assert count == 2
    assert len(statements) == 1
    assert "FROM vendor_test_recipient" in statements[0]
    assert "status='active'" in statements[0]
    assert "phone_hmac" not in statements[0]


def test_preflight_helper_only_invokes_zhihui_get_balance() -> None:
    calls: list[str] = []

    class Client:
        async def __aenter__(self) -> Client:
            calls.append("enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            calls.append("exit")

        async def get_balance(self) -> int:
            calls.append("get_balance")
            return 1234

        async def get_report(self) -> None:
            raise AssertionError("GetReport is consuming")

        async def get_reply(self) -> None:
            raise AssertionError("GetReply is consuming")

    probe = asyncio.run(
        manager_module.probe_balance_with_zhihui(
            lambda: Client(),
            correlation_id="preflight-1",
            clock=lambda: datetime(2026, 7, 16, 1, 2, 3, tzinfo=UTC),
        )
    )

    assert calls == ["enter", "get_balance", "exit"]
    assert probe.balance == 1234
    assert probe.code == 0


def test_host_balance_probe_exposes_only_integer_vendor_error_code() -> None:
    operations = object.__new__(manager_module.HostActivationOperations)
    operations.correlation_id = "activation-safe"
    operations._run = lambda *_args, **_kwargs: '{"code":1010}'  # type: ignore[method-assign]

    with pytest.raises(manager_module.VendorTestProbeRejected) as captured:
        operations.probe_balance()

    assert captured.value.vendor_code == 1010
    assert str(captured.value) == "vendor probe rejected: code=1010"


def test_vendor_probe_rejection_writes_safe_code_and_keeps_senders_stopped(
    tmp_path: Path,
) -> None:
    class RejectedOperations(FakeOperations):
        def probe_balance(self) -> manager_module.BalanceProbe:
            self._event("get_balance")
            raise manager_module.VendorTestProbeRejected(1010)

    operations = RejectedOperations()
    paths = _paths(tmp_path)

    with pytest.raises(manager_module.VendorTestProbeRejected) as captured:
        manager_module.VendorTestActivationManager(
            operations,
            paths,
            expected_uid=os.geteuid(),
        ).activate()

    evidence = json.loads(paths.evidence_file.read_text(encoding="utf-8"))
    assert captured.value.vendor_code == 1010
    assert evidence["vendor_code"] == 1010
    assert evidence["status"] == "blocked"
    assert evidence["step"] == "get_balance"
    assert "start_senders" not in operations.events
    assert operations.events[-1] == "hold_fail_closed"


def test_cli_reports_vendor_code_without_vendor_message() -> None:
    source = (ROOT / "deploy/scripts/vendor_test_manager.py").read_text(
        encoding="utf-8"
    )

    assert "except VendorTestProbeRejected as error:" in source
    assert 'vendor_code={error.vendor_code}' in source


def test_parser_accepts_fixed_pause_and_resume_commands() -> None:
    parser = manager_module._build_parser()
    common = [
        "--root",
        "/srv/sms-platform",
        "--runtime-root",
        "/run/sms-platform/secrets",
        "--state-dir",
        "/var/lib/sms-platform/vendor-test",
        "--marker-file",
        "/etc/sms-platform/test-environment",
        "--dotenv-file",
        "/srv/sms-platform/.env",
    ]

    assert parser.parse_args(["pause", *common]).command == "pause"
    assert parser.parse_args(["resume", *common]).command == "resume"
    assert parser.parse_args(["rotate", *common]).command == "rotate"
    assert parser.parse_args(["recover-rotation", *common]).command == "recover-rotation"


def test_critical_resume_probes_balance_before_clearing_pause() -> None:
    class PauseOperations:
        def __init__(self) -> None:
            self.events: list[str] = []

        def require_lifecycle_lock(self) -> None:
            self.events.append("lock")

        def current_pause_kind(self) -> str:
            self.events.append("pause_kind")
            return "critical"

        def set_manual_pause(self) -> None:
            raise AssertionError("resume must not set a pause")

        def probe_balance(self) -> manager_module.BalanceProbe:
            self.events.append("get_balance")
            return manager_module.BalanceProbe(
                0,
                "ok",
                100,
                "2026-07-17T09:00:00Z",
                "resume-1",
            )

        def clear_pause(self, pause_kind: str) -> None:
            self.events.append(f"clear_{pause_kind}")

    operations = PauseOperations()
    result = manager_module.VendorTestPauseManager(operations).resume()

    assert result.status == "resumed"
    assert result.pause_kind == "critical"
    assert operations.events == [
        "lock",
        "pause_kind",
        "get_balance",
        "clear_critical",
    ]


def test_rotation_failure_layers_critical_pause_over_existing_manual_pause() -> None:
    operations = object.__new__(manager_module.HostActivationOperations)
    values = {
        "queue:paused:realtime": "vendor-test-manual",
        "queue:paused:bulk": "vendor-test-manual",
    }

    def redis(*arguments: str) -> str:
        if arguments[0] == "GET":
            return values.get(arguments[1], "")
        if arguments[0] == "SET":
            key, value = arguments[1:3]
            if len(arguments) == 4 and arguments[3] == "NX" and key in values:
                return ""
            values[key] = value
            return "OK"
        if arguments[0] == "EVAL":
            assert arguments[2] == "6"
            keys = arguments[3:9]
            assert keys[-2:] == (
                "queue:paused:vendor-test-rotation-failed:realtime",
                "queue:paused:vendor-test-rotation-failed:bulk",
            )
            for key in keys[:2]:
                if values.get(key) != "vendor-test-manual":
                    values.pop(key, None)
            for key in keys[2:]:
                values.pop(key, None)
            return "1"
        raise AssertionError(arguments)

    operations._redis = redis  # type: ignore[method-assign]
    operations.stop_senders = lambda: None  # type: ignore[method-assign]

    operations.hold_fail_closed()

    assert operations.current_pause_kind() == "critical"
    assert values["queue:paused:realtime"] == "vendor-test-manual"
    assert values["queue:paused:bulk"] == "vendor-test-manual"

    operations.clear_pause("critical")

    assert operations.current_pause_kind() == "manual"
    assert values == {
        "queue:paused:realtime": "vendor-test-manual",
        "queue:paused:bulk": "vendor-test-manual",
    }


def test_manual_resume_never_clears_daily_or_critical_pause() -> None:
    class PauseOperations:
        def require_lifecycle_lock(self) -> None:
            pass

        def current_pause_kind(self) -> str:
            return "daily"

        def set_manual_pause(self) -> None:
            raise AssertionError

        def probe_balance(self) -> manager_module.BalanceProbe:
            raise AssertionError

        def clear_pause(self, pause_kind: str) -> None:
            raise AssertionError(f"must preserve {pause_kind}")

    with pytest.raises(manager_module.VendorTestActivationError, match="daily"):
        manager_module.VendorTestPauseManager(PauseOperations()).resume()


def test_host_budget_snapshot_queries_the_canonical_vendor_ledger() -> None:
    statements: list[str] = []
    operations = object.__new__(manager_module.HostActivationOperations)

    def psql(statement: str) -> str:
        statements.append(statement)
        return "0|0|0"

    operations._psql = psql  # type: ignore[method-assign]

    snapshot = operations.budget_snapshot()

    assert snapshot == manager_module.BudgetSnapshot(0, 0, 0)
    assert len(statements) == 1
    assert "FROM vendor_test_daily_usage" in statements[0]
    assert "FROM live_test_daily_usage" not in statements[0]


def test_vendor_state_directory_requires_backend_traverse_group(tmp_path: Path) -> None:
    state = tmp_path / "vendor-test"
    state.mkdir(mode=0o710)

    manager_module._private_directory(
        state,
        expected_uid=os.geteuid(),
        expected_mode=0o710,
        expected_gid=os.getegid(),
    )

    state.chmod(0o700)
    with pytest.raises(manager_module.VendorTestActivationError, match="unsafe"):
        manager_module._private_directory(
            state,
            expected_uid=os.geteuid(),
            expected_mode=0o710,
            expected_gid=os.getegid(),
        )
