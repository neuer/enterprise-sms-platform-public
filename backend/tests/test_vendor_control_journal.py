from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

OPERATION_ID = "c0a80101-0000-4000-8000-000000000081"
SENTINEL = "formal-key-private-13800138000-" + "a" * 64


def test_journal_persists_only_strict_safe_result_fields(tmp_path: Path) -> None:
    from vendor_control_journal import VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())

    running, created = journal.begin(OPERATION_ID, "activate")
    terminal = journal.finish(
        OPERATION_ID,
        "activate",
        status="failed",
        safe_code="VENDOR_ERROR",
        vendor_code=1010,
    )

    assert created is True and running.status == "running"
    assert terminal.status == "failed"
    assert terminal.vendor_code == 1010
    loaded = VendorControlJournal(
        tmp_path / "journal", expected_uid=os.getuid()
    ).get(OPERATION_ID)
    assert loaded == terminal
    path = tmp_path / "journal" / f"{OPERATION_ID}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "operation_id",
        "operation",
        "status",
        "safe_code",
        "checkpoint_id",
        "vendor_code",
        "phase",
        "recorded_at",
    }
    assert "payload" not in path.read_text(encoding="utf-8").lower()
    assert SENTINEL not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_journal_recovers_activation_checkpoint_after_response_loss(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "activate")

    terminal = journal.finish(
        OPERATION_ID,
        "activate",
        status="succeeded",
        safe_code=None,
        checkpoint_id="activation-20260717T090000Z",
    )

    assert terminal.checkpoint_id == "activation-20260717T090000Z"
    assert terminal.vendor_code is None


def test_journal_recovers_rotation_checkpoint_after_response_loss(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "rotate_credentials")

    terminal = journal.finish(
        OPERATION_ID,
        "rotate_credentials",
        status="succeeded",
        safe_code=None,
        checkpoint_id="vendor-rotation-20260717T090000Z",
    )

    assert terminal.checkpoint_id == "vendor-rotation-20260717T090000Z"
    assert journal.get(OPERATION_ID) == terminal


def test_journal_persists_reset_configuration_without_payload(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())

    running, created = journal.begin(OPERATION_ID, "reset_configuration")
    authorized = journal.authorize_reset(OPERATION_ID)
    revoked = journal.mark_runtime_revoked(OPERATION_ID)
    terminal = journal.finish(
        OPERATION_ID,
        "reset_configuration",
        status="succeeded",
        safe_code=None,
    )

    assert created is True
    assert running.operation == "reset_configuration"
    assert running.phase is None
    assert authorized.phase == "reset_authorized"
    assert revoked.phase == "runtime_revoked"
    assert terminal.status == "succeeded"
    assert terminal.phase == "runtime_revoked"
    assert terminal.checkpoint_id is None
    assert terminal.vendor_code is None
    document = json.loads(
        (tmp_path / "journal" / f"{OPERATION_ID}.json").read_text(encoding="utf-8")
    )
    assert document["operation"] == "reset_configuration"
    assert document["phase"] == "runtime_revoked"
    assert "payload" not in document


def test_reset_authorization_is_idempotent_and_rejects_wrong_journal_state(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalConflict, VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")

    first = journal.authorize_reset(OPERATION_ID)
    replay = journal.authorize_reset(OPERATION_ID)

    assert first == replay
    assert first.status == "running"
    assert first.phase == "reset_authorized"

    revoked = journal.mark_runtime_revoked(OPERATION_ID)
    assert revoked.phase == "runtime_revoked"
    assert journal.mark_runtime_revoked(OPERATION_ID) == revoked

    other_id = "c0a80101-0000-4000-8000-000000000082"
    journal.begin(other_id, "activate")
    with pytest.raises(JournalConflict):
        journal.authorize_reset(other_id)

    journal.finish(
        OPERATION_ID,
        "reset_configuration",
        status="succeeded",
        safe_code=None,
    )
    with pytest.raises(JournalConflict):
        journal.authorize_reset(OPERATION_ID)


def test_reset_authorization_fsyncs_file_before_atomic_replace_and_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from vendor_control_journal import VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(descriptor)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "replace", tracked_replace)

    authorized = journal.authorize_reset(OPERATION_ID)

    assert authorized.phase == "reset_authorized"
    assert events == ["fsync:file", "replace", "fsync:directory"]


def test_journal_replay_is_idempotent_and_conflicting_operation_is_rejected(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalConflict, VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    first, created = journal.begin(OPERATION_ID, "pause")
    replay, replay_created = journal.begin(OPERATION_ID, "pause")

    assert first == replay
    assert created is True and replay_created is False
    with pytest.raises(JournalConflict) as captured:
        journal.begin(OPERATION_ID, "resume")
    assert SENTINEL not in str(captured.value)


def test_journal_rejects_duplicate_keys_and_exposes_no_payload_parameter(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    path.write_text(
        '{"schema_version":1,"operation_id":"'
        + OPERATION_ID
        + '","operation":"activate","operation":"pause",'
        '"status":"running","safe_code":null,'
        '"recorded_at":"2026-07-17T09:00:00+00:00"}\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(JournalError) as captured:
        journal.get(OPERATION_ID)

    assert "payload" not in inspect.signature(journal.begin).parameters
    assert "payload" not in inspect.signature(journal.authorize_reset).parameters
    assert "payload" not in inspect.signature(journal.mark_runtime_revoked).parameters
    assert "payload" not in inspect.signature(journal.finish).parameters
    assert SENTINEL not in str(captured.value)


def test_journal_reads_legacy_v1_and_rejects_unknown_schema_version(tmp_path: Path) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": OPERATION_ID,
                "operation": "activate",
                "status": "running",
                "safe_code": None,
                "recorded_at": "2026-07-17T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    legacy = journal.get(OPERATION_ID)

    assert legacy is not None
    assert legacy.checkpoint_id is None and legacy.vendor_code is None
    assert legacy.phase is None

    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 4
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(JournalError, match="schema"):
        journal.get(OPERATION_ID)


def test_journal_reads_legacy_v2_terminal_without_reset_phase(tmp_path: Path) -> None:
    from vendor_control_journal import VendorControlJournal

    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "operation_id": OPERATION_ID,
                "operation": "reset_configuration",
                "status": "succeeded",
                "safe_code": None,
                "checkpoint_id": None,
                "vendor_code": None,
                "recorded_at": "2026-07-17T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    legacy = journal.get(OPERATION_ID)

    assert legacy is not None
    assert legacy.status == "succeeded"
    assert legacy.phase is None


def test_runtime_revoked_requires_authorized_running_reset(tmp_path: Path) -> None:
    from vendor_control_journal import JournalConflict, VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")

    with pytest.raises(JournalConflict):
        journal.mark_runtime_revoked(OPERATION_ID)

    journal.authorize_reset(OPERATION_ID)
    revoked = journal.mark_runtime_revoked(OPERATION_ID)
    assert revoked.status == "running"
    assert revoked.phase == "runtime_revoked"

    other_id = "c0a80101-0000-4000-8000-000000000089"
    journal.begin(other_id, "activate")
    with pytest.raises(JournalConflict):
        journal.mark_runtime_revoked(other_id)


def test_runtime_revoked_fsync_failure_keeps_authorized_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")
    journal.authorize_reset(OPERATION_ID)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected phase replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(JournalError, match="runtime"):
        journal.mark_runtime_revoked(OPERATION_ID)

    assert journal.get(OPERATION_ID).phase == "reset_authorized"  # type: ignore[union-attr]


def test_current_schema_reset_success_requires_runtime_revoked_phase(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalConflict, VendorControlJournal

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")
    journal.authorize_reset(OPERATION_ID)

    with pytest.raises(JournalConflict, match="runtime"):
        journal.finish(
            OPERATION_ID,
            "reset_configuration",
            status="succeeded",
            safe_code=None,
        )


@pytest.mark.parametrize("schema_version", (True, 3.0))
def test_journal_rejects_non_integer_schema_versions(
    tmp_path: Path,
    schema_version: object,
) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    document = {
        "schema_version": schema_version,
        "operation_id": OPERATION_ID,
        "operation": "activate",
        "status": "running",
        "safe_code": None,
        "recorded_at": "2026-07-17T09:00:00+00:00",
    }
    if schema_version == 3.0:
        document.update(
            {
                "checkpoint_id": None,
                "vendor_code": None,
                "phase": None,
            }
        )
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(JournalError, match="schema"):
        journal.get(OPERATION_ID)


@pytest.mark.parametrize(
    ("operation", "phase"),
    (
        ("reset_configuration", "reset_started_unknown"),
        ("reset_configuration", 1),
        ("activate", "reset_authorized"),
    ),
)
def test_journal_rejects_unknown_or_cross_operation_reset_phase(
    tmp_path: Path,
    operation: str,
    phase: object,
) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "operation_id": OPERATION_ID,
                "operation": operation,
                "status": "running",
                "safe_code": None,
                "checkpoint_id": None,
                "vendor_code": None,
                "phase": phase,
                "recorded_at": "2026-07-17T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(JournalError, match="phase"):
        journal.get(OPERATION_ID)


def test_journal_rejects_operation_id_mismatch_between_filename_and_record(
    tmp_path: Path,
) -> None:
    from vendor_control_journal import JournalError, VendorControlJournal

    embedded_id = "c0a80101-0000-4000-8000-000000000083"
    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{OPERATION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "operation_id": embedded_id,
                "operation": "reset_configuration",
                "status": "running",
                "safe_code": None,
                "checkpoint_id": None,
                "vendor_code": None,
                "phase": "reset_authorized",
                "recorded_at": "2026-07-17T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(JournalError, match="operation ID"):
        journal.get(OPERATION_ID)


def test_agent_restart_reads_terminal_journal_without_repeating_wrapper(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStatus

    from vendor_control_protocol import ControlRequest

    class Runner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, operation: str) -> agent_module.WrapperResult:
            self.calls.append(operation)
            return agent_module.WrapperResult(
                0,
                None,
                {"checkpoint_id": "activation-20260717T090000Z"},
            )

    class Store:
        def status(self) -> CredentialStatus:
            return CredentialStatus(False, "setup_required", None)

    class Sessions:
        pass

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    first_runner = Runner()
    first_agent = agent_module.VendorControlAgent(
        runner=first_runner,
        credential_store=Store(),
        seal_sessions=Sessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    )
    activate = ControlRequest(OPERATION_ID, "activate", {})

    first = first_agent.handle(
        activate,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )
    restarted_runner = Runner()
    restarted_agent = agent_module.VendorControlAgent(
        runner=restarted_runner,
        credential_store=Store(),
        seal_sessions=Sessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    )
    recovered = restarted_agent.handle(
        ControlRequest(OPERATION_ID, "status", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert first.status == "ok"
    assert first_runner.calls == ["activate"]
    assert recovered.body == {
        "operation_status": "succeeded",
        "checkpoint_id": "activation-20260717T090000Z",
    }
    assert restarted_runner.calls == []


def test_agent_restart_recovers_integer_vendor_code_without_vendor_message(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStatus

    from vendor_control_protocol import ControlRequest

    class Runner:
        def run(self, _operation: str) -> agent_module.WrapperResult:
            return agent_module.WrapperResult(
                1,
                "VENDOR_ERROR",
                {"vendor_code": 1010},
            )

    class Store:
        def status(self) -> CredentialStatus:
            return CredentialStatus(False, "setup_required", None)

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    agent = agent_module.VendorControlAgent(
        runner=Runner(),
        credential_store=Store(),
        seal_sessions=object(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    )
    request = ControlRequest(OPERATION_ID, "activate", {})
    agent.handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())

    recovered = agent_module.VendorControlAgent(
        runner=Runner(),
        credential_store=Store(),
        seal_sessions=object(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(OPERATION_ID, "status", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert recovered.safe_code == "VENDOR_ERROR"
    assert recovered.body == {"operation_status": "failed", "vendor_code": 1010}
    assert "carrier" not in repr(recovered).casefold()


def test_agent_status_distinguishes_missing_and_interrupted_journal(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStatus

    from vendor_control_protocol import ControlRequest

    class Runner:
        def run(self, _operation: str) -> agent_module.WrapperResult:
            raise AssertionError("status must not execute wrapper")

    class Store:
        def status(self) -> CredentialStatus:
            return CredentialStatus(False, "setup_required", None)

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    agent = agent_module.VendorControlAgent(
        runner=Runner(),
        credential_store=Store(),
        seal_sessions=object(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    )
    missing = agent.handle(
        ControlRequest(OPERATION_ID, "status", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )
    journal.begin(OPERATION_ID, "activate")
    interrupted = agent.handle(
        ControlRequest(OPERATION_ID, "status", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert missing.safe_code == "OPERATION_NOT_FOUND"
    assert missing.body == {"operation_status": "not_found"}
    assert interrupted.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert interrupted.body == {"operation_status": "failed"}


def test_agent_status_keeps_interrupted_reset_reconcilable(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStatus

    from vendor_control_protocol import ControlRequest

    class Store:
        def status(self) -> CredentialStatus:
            return CredentialStatus(False, "setup_required", None)

    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(OPERATION_ID, "reset_configuration")
    response = agent_module.VendorControlAgent(
        runner=object(),
        credential_store=Store(),
        seal_sessions=object(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(OPERATION_ID, "status", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_IN_PROGRESS"
    assert response.body == {"operation_status": "running"}
