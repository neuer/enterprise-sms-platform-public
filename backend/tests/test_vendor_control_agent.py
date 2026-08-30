from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
SECRET_FIXTURE = "formal-vendor-private-value-never-log"


class FakeRunner:
    def __init__(
        self,
        returncode: int = 0,
        *,
        safe_code: str | None = None,
        body: dict[str, object] | None = None,
        runtime_returncode: int = 0,
        runtime_effect: Callable[[], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self.safe_code = safe_code
        self.body = body or {}
        self.runtime_returncode = runtime_returncode
        self.runtime_effect = runtime_effect
        self.calls: list[str] = []

    def run(self, operation: str):
        import vendor_control_agent as agent_module

        self.calls.append(operation)
        if operation == "reset-runtime":
            if self.runtime_returncode == 0 and self.runtime_effect is not None:
                self.runtime_effect()
            return agent_module.WrapperResult(
                self.runtime_returncode,
                None if self.runtime_returncode == 0 else "CONTROL_COMMAND_FAILED",
                {"runtime_revoked": True} if self.runtime_returncode == 0 else {},
            )
        return agent_module.WrapperResult(
            self.returncode,
            self.safe_code,
            self.body,
        )


def _host_state(
    mode: str = "inactive",
    *,
    pause_kind: str | None = None,
) -> dict[str, object]:
    return {
        "mode": mode,
        "pause_kind": pause_kind,
        "active_recipient_count": 1,
    }


class FakeCredentialStore:
    def __init__(
        self,
        configured: bool = False,
        *,
        reset_needed: bool | None = None,
    ) -> None:
        self.configured = configured
        self.reset_needed = configured if reset_needed is None else reset_needed
        self.events: list[str] = []

    def status(self):
        from vendor_credential_store import CredentialStatus

        return CredentialStatus(
            self.configured,
            "active" if self.configured else "setup_required",
            None,
        )

    def install(self, _credentials: object):
        self.events.append("install")
        self.configured = True
        return self.status()

    def stage(self, _credentials: object):
        self.events.append("stage")
        return self.status()

    def discard_pending(self) -> None:
        self.events.append("discard_pending")

    def reset_required(self) -> bool:
        return self.reset_needed

    def reset(self):
        self.events.append("reset")
        self.configured = False
        self.reset_needed = False
        return self.status()

    def revoke_from_runtime(self) -> None:
        self.configured = False
        self.reset_needed = False


class FakeSealSessions:
    def open(self, _envelope: object, *, operation: str, actor: str):
        assert operation in {"install_credentials", "rotate_credentials"}
        assert actor == "admin"
        return SimpleNamespace(secret_name="name", secret_key="key")


def _agent(*, runner: FakeRunner | None = None):
    import vendor_control_agent as agent_module

    return agent_module.VendorControlAgent(
        runner=runner or FakeRunner(),
        credential_store=FakeCredentialStore(),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )


def test_agent_rejects_unapproved_peer_uid_or_gid() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    agent = _agent()
    request = ControlRequest(
        "c0a80101-0000-4000-8000-000000000001",
        "health",
        {},
    )

    with pytest.raises(agent_module.PeerDenied):
        agent.handle(request, peer_uid=os.getuid() + 1, peer_gid=os.getgid())
    with pytest.raises(agent_module.PeerDenied):
        agent.handle(request, peer_uid=os.getuid(), peer_gid=os.getgid() + 1)


def test_worker_peer_is_limited_to_status_only_operations() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    worker_uid = os.getuid() + 1
    agent = agent_module.VendorControlAgent(
        runner=FakeRunner(),
        credential_store=FakeCredentialStore(),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        status_only_uid=worker_uid,
        status_only_gid=os.getgid(),
    )

    health = agent.handle(
        ControlRequest("c0a80101-0000-4000-8000-000000000001", "health", {}),
        peer_uid=worker_uid,
        peer_gid=os.getgid(),
    )
    assert health.status == "ok"

    with pytest.raises(agent_module.PeerDenied):
        agent.handle(
            ControlRequest("c0a80101-0000-4000-8000-000000000002", "activate", {}),
            peer_uid=worker_uid,
            peer_gid=os.getgid(),
        )


@pytest.mark.parametrize("operation", ("activate", "pause", "resume"))
def test_agent_dispatches_only_fixed_wrapper_operations(operation: str) -> None:
    from vendor_control_protocol import ControlRequest

    runner = FakeRunner()
    agent = _agent(runner=runner)
    body = {"pause_kind": "manual"} if operation in {"pause", "resume"} else {}
    if operation == "resume":
        agent._pause_kind = "manual"

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000001",
            operation,
            body,
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert runner.calls == [operation]


def test_operation_id_is_idempotent_and_conflicting_reuse_is_rejected() -> None:
    from vendor_control_protocol import ControlRequest

    runner = FakeRunner()
    agent = _agent(runner=runner)
    operation_id = "c0a80101-0000-4000-8000-000000000001"
    activate = ControlRequest(operation_id, "activate", {})

    first = agent.handle(activate, peer_uid=os.getuid(), peer_gid=os.getgid())
    replay = agent.handle(activate, peer_uid=os.getuid(), peer_gid=os.getgid())
    conflict = agent.handle(
        ControlRequest(operation_id, "pause", {"pause_kind": "manual"}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert first == replay
    assert runner.calls == ["activate"]
    assert conflict.status == "error"
    assert conflict.safe_code == "OPERATION_ID_CONFLICT"


def test_fixed_runner_never_accepts_or_appends_caller_arguments() -> None:
    import vendor_control_agent as agent_module

    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            '{"checkpoint_id":"checkpoint-1","status":"activated"}\n',
            "",
        )

    runner = agent_module.FixedWrapperRunner(command_runner=run)

    assert runner.run("activate").returncode == 0
    assert calls[0][0] == [
        "/usr/local/sbin/sms-compose",
        "vendor-test",
        "activate",
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 180
    assert runner.run("reset-runtime").returncode == 1
    assert calls[1][0] == [
        agent_module.sys.executable,
        agent_module.LIFECYCLE_LOCK_RUNNER,
        "--runtime-root",
        agent_module.RUNTIME_ROOT,
        "--wrapper",
        agent_module.MOCK_RESET_WRAPPER,
        "--operation",
        "vendor-test",
        "--",
        "reset-to-mock",
    ]
    assert calls[1][1]["timeout"] == 300
    with pytest.raises(agent_module.UnsupportedAgentOperation):
        runner.run("status --path /tmp")


def test_fixed_runner_treats_wrapper_timeout_as_failure() -> None:
    import vendor_control_agent as agent_module

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=float(kwargs["timeout"]))

    result = agent_module.FixedWrapperRunner(command_runner=run).run("activate")
    assert result.returncode == 1
    assert result.safe_code == "CONTROL_COMMAND_FAILED"


def test_rotate_stages_credentials_then_runs_fixed_lifecycle_wrapper() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(body={"checkpoint_id": "rotation-checkpoint-1"})
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000019",
            "rotate_credentials",
            {
                "actor": "admin",
                "session_id": "seal-1",
                "wrapped_key": "wrapped",
                "nonce": "nonce",
                "ciphertext": "ciphertext",
                "aad": "aad",
                "algorithm": "RSA-OAEP-256+A256GCM",
            },
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert response.body["checkpoint_id"] == "rotation-checkpoint-1"
    assert store.events == ["stage"]
    assert runner.calls == ["recover-rotation", "rotate"]


def test_rotate_discards_staged_candidate_if_wrapper_cannot_start() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    class UnavailableRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, operation: str):
            self.calls.append(operation)
            if operation == "recover-rotation":
                return agent_module.WrapperResult(0, None, {})
            raise OSError("wrapper unavailable")

    store = FakeCredentialStore(configured=True)
    runner = UnavailableRunner()
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000020",
            "rotate_credentials",
            {
                "actor": "admin",
                "session_id": "seal-1",
                "wrapped_key": "wrapped",
                "nonce": "nonce",
                "ciphertext": "ciphertext",
                "aad": "aad",
                "algorithm": "RSA-OAEP-256+A256GCM",
            },
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == ["stage", "discard_pending"]
    assert runner.calls == ["recover-rotation", "rotate"]


def test_reset_configuration_is_journaled_and_replay_does_not_reset_again(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000021"
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    first_agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    )
    request = ControlRequest(operation_id, "reset_configuration", {})

    first = first_agent.handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )
    replay = agent_module.VendorControlAgent(
        runner=FakeRunner(),
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert first.status == "ok"
    assert replay.status == "ok"
    assert replay.body == {"operation_status": "succeeded"}
    assert store.events == []
    recorded = journal.get(operation_id)
    assert recorded is not None
    assert recorded.operation == "reset_configuration"
    assert recorded.status == "succeeded"
    assert recorded.phase == "runtime_revoked"
    assert runner.calls == ["status", "reset-runtime"]


def test_reset_configuration_authorizes_wrapper_before_marking_runtime_revoked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStatus

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000221"
    events: list[str] = []

    class Runner(FakeRunner):
        def run(self, operation: str):
            events.append(operation)
            return super().run(operation)

    class Store(FakeCredentialStore):
        def reset(self):
            raise AssertionError("agent must not delete the root credential store")

        def revoke_from_runtime(self) -> None:
            events.append("runtime_store_reset")
            super().revoke_from_runtime()

    store = Store(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    real_authorize = journal.authorize_reset
    real_mark = journal.mark_runtime_revoked

    def authorize_reset(value: str):
        record = real_authorize(value)
        events.append("authorize")
        return record

    def mark_runtime_revoked(value: str):
        record = real_mark(value)
        events.append("mark")
        return record

    monkeypatch.setattr(journal, "authorize_reset", authorize_reset)
    monkeypatch.setattr(journal, "mark_runtime_revoked", mark_runtime_revoked)
    response = agent_module.VendorControlAgent(
        runner=Runner(
            body=_host_state(),
            runtime_effect=store.revoke_from_runtime,
        ),
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert events == [
        "status",
        "authorize",
        "reset-runtime",
        "runtime_store_reset",
        "mark",
    ]
    assert store.status() == CredentialStatus(False, "setup_required", None)
    assert journal.get(operation_id).phase == "runtime_revoked"  # type: ignore[union-attr]


def test_runtime_wrapper_failure_stays_running_and_replays_same_operation(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000222"
    request = ControlRequest(operation_id, "reset_configuration", {})
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())

    first = agent_module.VendorControlAgent(
        runner=FakeRunner(body=_host_state(), runtime_returncode=1),
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())

    replay_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    replay = agent_module.VendorControlAgent(
        runner=replay_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())

    assert first.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert first.body == {"operation_status": "running"}
    assert replay.status == "ok"
    assert store.events == []
    assert replay_runner.calls == ["status", "reset-runtime"]
    assert journal.get(operation_id).status == "succeeded"  # type: ignore[union-attr]
    assert journal.get(operation_id).phase == "runtime_revoked"  # type: ignore[union-attr]


def test_runtime_phase_write_failure_replays_probe_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import JournalError, VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000223"
    request = ControlRequest(operation_id, "reset_configuration", {})
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    real_mark = journal.mark_runtime_revoked
    marks = 0

    def fail_once(value: str):
        nonlocal marks
        marks += 1
        if marks == 1:
            raise JournalError("private runtime phase detail")
        return real_mark(value)

    monkeypatch.setattr(journal, "mark_runtime_revoked", fail_once)
    first_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    first = agent_module.VendorControlAgent(
        runner=first_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())
    replay_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    replay = agent_module.VendorControlAgent(
        runner=replay_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())

    assert first.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert first.body == {"operation_status": "running"}
    assert replay.status == "ok"
    assert first_runner.calls == ["status", "reset-runtime"]
    assert replay_runner.calls == ["status", "reset-runtime"]
    assert marks == 2


def test_runtime_revoked_phase_still_requires_current_store_and_runtime_probe(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000224"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    journal.authorize_reset(operation_id)
    journal.mark_runtime_revoked(operation_id)
    runner = FakeRunner(body=_host_state())
    store = FakeCredentialStore(configured=False)

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert runner.calls == ["status", "reset-runtime"]


def test_new_reset_rejects_unconfigured_store_even_when_host_is_inactive() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=False)
    runner = FakeRunner(body=_host_state())
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000022",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == []


def test_new_reset_journal_does_not_authorize_unconfigured_replay(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000122"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    store = FakeCredentialStore(configured=False)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == []
    recorded = journal.get(operation_id)
    assert recorded is not None and recorded.status == "failed"


def test_interrupted_reset_before_authorization_rejects_unconfigured_replay(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000023"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    store = FakeCredentialStore(configured=False, reset_needed=True)

    runner = FakeRunner(body=_host_state())
    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    recorded = journal.get(operation_id)
    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == []
    assert recorded is not None and recorded.status == "failed"


def test_reset_rejects_authorized_phase_bound_to_different_operation_id(
    tmp_path: Path,
) -> None:
    import json

    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000133"
    embedded_id = "c0a80101-0000-4000-8000-000000000134"
    root = tmp_path / "journal"
    journal = VendorControlJournal(root, expected_uid=os.getuid())
    journal.ensure_root()
    path = root / f"{operation_id}.json"
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
    store = FakeCredentialStore(configured=False, reset_needed=True)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == []


def test_interrupted_reset_after_authorization_replays_partial_inventory(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000125"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    journal.authorize_reset(operation_id)
    store = FakeCredentialStore(configured=False, reset_needed=True)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    recorded = journal.get(operation_id)
    assert response.status == "ok"
    assert response.body == {"operation_status": "succeeded"}
    assert store.events == []
    assert runner.calls == ["status", "reset-runtime"]
    assert recorded is not None and recorded.status == "succeeded"
    assert recorded.phase == "runtime_revoked"


def test_legacy_running_reset_with_configured_store_reauthorizes_before_wrapper(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000128"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    running, _ = journal.begin(operation_id, "reset_configuration")
    assert running.phase is None
    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    recorded = journal.get(operation_id)
    assert response.status == "ok"
    assert store.events == []
    assert runner.calls == ["status", "reset-runtime"]
    assert recorded is not None
    assert recorded.status == "succeeded"
    assert recorded.phase == "runtime_revoked"


@pytest.mark.parametrize(
    ("returncode", "body"),
    (
        (0, _host_state("blocked", pause_kind="critical")),
        (1, {}),
        (0, {}),
    ),
)
def test_authorized_reset_stays_running_for_unverified_or_blocked_host(
    tmp_path: Path,
    returncode: int,
    body: dict[str, object],
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000123"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    journal.authorize_reset(operation_id)
    store = FakeCredentialStore(configured=False, reset_needed=True)
    runner = FakeRunner(returncode, body=body)

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert response.body == {"operation_status": "running"}
    assert store.events == []
    assert runner.calls == ["status"]
    recorded = journal.get(operation_id)
    assert recorded is not None and recorded.status == "running"
    assert recorded.phase == "reset_authorized"


def test_authorized_reset_runs_while_host_is_controlled(tmp_path: Path) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000132"
    request = ControlRequest(operation_id, "reset_configuration", {})
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    journal.authorize_reset(operation_id)
    store = FakeCredentialStore(configured=False, reset_needed=True)

    runner = FakeRunner(body=_host_state("controlled"))
    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(request, peer_uid=os.getuid(), peer_gid=os.getgid())

    assert response.status == "ok"
    assert response.body == {"operation_status": "succeeded"}
    assert runner.calls == ["status", "reset-runtime"]
    assert store.events == []


def test_authorized_reset_stays_running_for_corrupt_credential_inventory(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal
    from vendor_credential_store import CredentialStoreError

    from vendor_control_protocol import ControlRequest

    class CorruptCredentialStore(FakeCredentialStore):
        def status(self):
            raise CredentialStoreError("private-corrupt-inventory-detail")

    operation_id = "c0a80101-0000-4000-8000-000000000124"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    journal.begin(operation_id, "reset_configuration")
    journal.authorize_reset(operation_id)
    store = CorruptCredentialStore(configured=False, reset_needed=True)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert response.body == {"operation_status": "running"}
    assert store.events == []
    assert runner.calls == []
    assert "private-corrupt-inventory-detail" not in repr(response)
    recorded = journal.get(operation_id)
    assert recorded is not None and recorded.status == "running"
    assert recorded.phase == "reset_authorized"


@pytest.mark.parametrize(
    ("returncode", "body"),
    (
        (0, _host_state("blocked", pause_kind="critical")),
        (1, {}),
        (0, {}),
    ),
)
def test_reset_configuration_rejects_stale_or_unverified_host_mode(
    tmp_path: Path,
    returncode: int,
    body: dict[str, object],
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(returncode, body=body)
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=VendorControlJournal(
            tmp_path / "journal",
            expected_uid=os.getuid(),
        ),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000023",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == ["status"]


@pytest.mark.parametrize("mode", ("inactive", "controlled"))
def test_reset_configuration_refreshes_safe_host_before_wrapper(
    tmp_path: Path,
    mode: str,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(
        body=_host_state(mode),
        runtime_effect=store.revoke_from_runtime,
    )
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=VendorControlJournal(
            tmp_path / "journal",
            expected_uid=os.getuid(),
        ),
    )
    agent._mode = "blocked"
    agent._pause_kind = "critical"

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000027",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert store.events == []
    assert runner.calls == ["status", "reset-runtime"]


def test_configured_reset_without_durable_journal_fails_closed() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    ).handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000130",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == []


def test_reset_authorization_phase_is_persisted_before_runtime_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    events: list[str] = []

    class TrackingRunner(FakeRunner):
        def run(self, operation: str):
            events.append(operation)
            return super().run(operation)

    operation_id = "c0a80101-0000-4000-8000-000000000126"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    real_authorize = journal.authorize_reset

    def authorize_reset(value: str):
        record = real_authorize(value)
        events.append("authorize")
        return record

    monkeypatch.setattr(journal, "authorize_reset", authorize_reset)
    store = FakeCredentialStore(configured=True)
    runner = TrackingRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert events == ["status", "authorize", "reset-runtime"]
    assert store.events == []


def test_reset_authorization_phase_write_failure_prevents_runtime_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import JournalError, VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000127"
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())

    def fail_authorization(_operation_id: str):
        raise JournalError("private-phase-write-detail")

    monkeypatch.setattr(journal, "authorize_reset", fail_authorization)
    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(body=_host_state())

    response = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
    ).handle(
        ControlRequest(operation_id, "reset_configuration", {}),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []
    assert runner.calls == ["status"]
    assert "private-phase-write-detail" not in repr(response)


def test_reset_configuration_requires_empty_body() -> None:
    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    agent = agent_module.VendorControlAgent(
        runner=FakeRunner(),
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000024",
            "reset_configuration",
            {"unexpected": "must-not-be-forwarded"},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_OPERATION_REJECTED"
    assert store.events == []


def test_reset_configuration_persists_safe_setup_required_projection(
    tmp_path: Path,
) -> None:
    import json

    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    state_path = tmp_path / "control-state.json"
    runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=VendorControlJournal(
            tmp_path / "journal",
            expected_uid=os.getuid(),
        ),
        state_path=state_path,
        state_expected_uid=os.getuid(),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000025",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert response.status == "ok"
    assert state["mode"] == "setup_required"
    assert state["credential_configured"] is False
    assert state["active_recipient_count"] == 1
    assert state["pause_kind"] is None


def test_reset_configuration_reports_control_state_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    store = FakeCredentialStore(configured=True)
    runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=VendorControlJournal(
            tmp_path / "journal",
            expected_uid=os.getuid(),
        ),
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )

    def fail_state_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("private-state-write-detail")

    monkeypatch.setattr(agent_module, "write_control_state", fail_state_write)
    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000026",
            "reset_configuration",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "CONTROL_STATE_SYNC_FAILED"
    assert response.body == {"operation_status": "running"}
    assert store.events == []
    assert "private-state-write-detail" not in repr(response)


def test_reset_state_failure_journal_replay_converges_without_second_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000028"
    request = ControlRequest(operation_id, "reset_configuration", {})
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    state_path = tmp_path / "control-state.json"
    runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    real_write = agent_module.write_control_state
    writes = 0

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("private-state-write-detail")
        real_write(*args, **kwargs)

    monkeypatch.setattr(agent_module, "write_control_state", fail_once)
    first = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
        state_path=state_path,
        state_expected_uid=os.getuid(),
    ).handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )
    replay_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    replay = agent_module.VendorControlAgent(
        runner=replay_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
        state_path=state_path,
        state_expected_uid=os.getuid(),
    ).handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    recorded = journal.get(operation_id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first.safe_code == "CONTROL_STATE_SYNC_FAILED"
    assert first.body == {"operation_status": "running"}
    assert replay.status == "ok"
    assert replay.body == {"operation_status": "succeeded"}
    assert recorded is not None
    assert recorded.status == "succeeded"
    assert recorded.safe_code is None
    assert store.events == []
    assert runner.calls == ["status", "reset-runtime", "status"]
    assert replay_runner.calls == ["status", "reset-runtime", "status"]
    assert writes == 2
    assert state["mode"] == "setup_required"
    assert state["credential_configured"] is False
    assert state["active_recipient_count"] == 1
    assert state["pause_kind"] is None


def test_reset_state_failure_remains_running_across_restarts_without_second_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000029"
    request = ControlRequest(operation_id, "reset_configuration", {})
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())

    def fail_state_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("private-state-write-detail")

    monkeypatch.setattr(agent_module, "write_control_state", fail_state_write)
    responses = []
    for runner in (
        FakeRunner(
            body=_host_state(),
            runtime_effect=store.revoke_from_runtime,
        ),
        FakeRunner(
            body=_host_state(),
            runtime_effect=store.revoke_from_runtime,
        ),
    ):
        responses.append(
            agent_module.VendorControlAgent(
                runner=runner,
                credential_store=store,
                seal_sessions=FakeSealSessions(),
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                journal=journal,
                state_path=tmp_path / "control-state.json",
                state_expected_uid=os.getuid(),
            ).handle(
                request,
                peer_uid=os.getuid(),
                peer_gid=os.getgid(),
            )
        )

    recorded = journal.get(operation_id)
    assert [response.safe_code for response in responses] == [
        "CONTROL_STATE_SYNC_FAILED",
        "CONTROL_STATE_SYNC_FAILED",
    ]
    assert all(
        response.body == {"operation_status": "running"}
        for response in responses
    )
    assert recorded is not None and recorded.status == "running"
    assert store.events == []


def test_reset_journal_finish_failure_is_recoverable_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module
    from vendor_control_journal import JournalError, VendorControlJournal

    from vendor_control_protocol import ControlRequest

    operation_id = "c0a80101-0000-4000-8000-000000000030"
    request = ControlRequest(operation_id, "reset_configuration", {})
    store = FakeCredentialStore(configured=True)
    journal = VendorControlJournal(tmp_path / "journal", expected_uid=os.getuid())
    real_finish = journal.finish
    finishes = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal finishes
        finishes += 1
        if finishes == 1:
            raise JournalError("private-journal-detail")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(journal, "finish", fail_once)
    first_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    first_agent = agent_module.VendorControlAgent(
        runner=first_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )
    first = first_agent.handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )
    replay_runner = FakeRunner(
        body=_host_state(),
        runtime_effect=store.revoke_from_runtime,
    )
    replay = agent_module.VendorControlAgent(
        runner=replay_runner,
        credential_store=store,
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        journal=journal,
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    ).handle(
        request,
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    recorded = journal.get(operation_id)
    assert first.safe_code == "CONTROL_RESULT_UNKNOWN"
    assert first.body == {"operation_status": "running"}
    assert replay.status == "ok"
    assert replay.body == {"operation_status": "succeeded"}
    assert recorded is not None and recorded.status == "succeeded"
    assert store.events == []
    assert operation_id not in first_agent._completed


def test_fixed_runner_accepts_only_safe_rotation_recovery_result() -> None:
    import vendor_control_agent as agent_module

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, '{"status":"recovered"}\n', "")

    result = agent_module.FixedWrapperRunner(command_runner=run).run("recover-rotation")

    assert result == agent_module.WrapperResult(0, None, {})


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_returncode"),
    [
        (0, '{"status":"runtime_revoked"}\n', 0),
        (0, '{"status":"runtime_revoked","target":"private"}\n', 1),
        (0, '{"status":"rotated"}\n', 1),
        (0, f"{SECRET_FIXTURE}\n" + '{"status":"runtime_revoked"}\n', 1),
        (1, '{"status":"runtime_revoked"}\n', 1),
        (0, "not-json\n", 1),
    ],
)
def test_fixed_runner_accepts_only_exact_runtime_revocation_projection(
    returncode: int,
    stdout: str,
    expected_returncode: int,
) -> None:
    import vendor_control_agent as agent_module

    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, SECRET_FIXTURE)

    result = agent_module.FixedWrapperRunner(command_runner=run).run("reset-runtime")

    assert calls == [
        [
            agent_module.sys.executable,
            agent_module.LIFECYCLE_LOCK_RUNNER,
            "--runtime-root",
            agent_module.RUNTIME_ROOT,
            "--wrapper",
            agent_module.MOCK_RESET_WRAPPER,
            "--operation",
            "vendor-test",
            "--",
            "reset-to-mock",
        ]
    ]
    assert result.returncode == expected_returncode
    assert result.body == ({"runtime_revoked": True} if expected_returncode == 0 else {})
    assert SECRET_FIXTURE not in repr(result)


@pytest.mark.parametrize(
    ("status", "pause_kind", "expected_mode"),
    [
        ("inactive", None, "inactive"),
        ("controlled", None, "controlled"),
        ("blocked", "manual", "blocked"),
        ("blocked", "critical", "blocked"),
        ("blocked", "daily", "blocked"),
    ],
)
def test_fixed_runner_parses_only_safe_host_status_projection(
    status: str,
    pause_kind: str | None,
    expected_mode: str,
) -> None:
    import vendor_control_agent as agent_module

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            __import__("json").dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "recipient_count": 1,
                    "pause_kind": pause_kind,
                    "actual_migration_head": "0018_vendor_code",
                    **(
                        {}
                        if status == "inactive"
                        else {
                            "mode": "development-vendor-live",
                            "vendor_origin": "https://vendor.example.invalid",
                            "daily_segment_limit": 100,
                            "timezone": "Asia/Shanghai",
                        }
                    ),
                }
            )
            + "\n",
            "",
        )

    result = agent_module.FixedWrapperRunner(command_runner=run).run("status")

    assert result.returncode == 0
    assert result.body == {
        "mode": expected_mode,
        "pause_kind": pause_kind,
        "active_recipient_count": 1,
    }


@pytest.mark.parametrize("pause_kind", [None, "manual", "critical", "daily"])
def test_agent_restores_controlled_and_paused_state_from_fixed_host_probe(
    tmp_path: Path,
    pause_kind: str | None,
) -> None:
    import vendor_control_agent as agent_module

    mode = "controlled" if pause_kind is None else "blocked"
    runner = FakeRunner(
        body={
            "mode": mode,
            "pause_kind": pause_kind,
            "active_recipient_count": 1,
        }
    )
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=FakeCredentialStore(configured=True),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )

    agent.write_heartbeat()

    state = __import__("json").loads(
        (tmp_path / "control-state.json").read_text(encoding="utf-8")
    )
    assert state["mode"] == mode
    assert state["pause_kind"] == pause_kind
    assert state["active_recipient_count"] == 1


def test_unconfigured_heartbeat_projects_setup_only_after_inactive_host_probe(
    tmp_path: Path,
) -> None:
    import json

    import vendor_control_agent as agent_module

    runner = FakeRunner(body=_host_state("inactive"))
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=FakeCredentialStore(configured=False),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )

    agent.write_heartbeat()

    state = json.loads(
        (tmp_path / "control-state.json").read_text(encoding="utf-8")
    )
    assert runner.calls == ["status"]
    assert state["mode"] == "setup_required"
    assert state["credential_configured"] is False
    assert state["active_recipient_count"] == 1
    assert state["pause_kind"] is None


@pytest.mark.parametrize(
    ("returncode", "body", "expected_mode", "expected_pause"),
    (
        (0, _host_state("controlled"), "controlled", None),
        (
            0,
            _host_state("blocked", pause_kind="critical"),
            "blocked",
            "critical",
        ),
        (1, {}, "blocked", "critical"),
        (0, {}, "blocked", "critical"),
    ),
)
def test_unconfigured_heartbeat_never_projects_setup_for_unsafe_host_state(
    tmp_path: Path,
    returncode: int,
    body: dict[str, object],
    expected_mode: str,
    expected_pause: str | None,
) -> None:
    import json

    import vendor_control_agent as agent_module

    runner = FakeRunner(returncode, body=body)
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=FakeCredentialStore(configured=False),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )

    agent.write_heartbeat()

    state = json.loads(
        (tmp_path / "control-state.json").read_text(encoding="utf-8")
    )
    assert runner.calls == ["status"]
    assert state["mode"] == expected_mode
    assert state["credential_configured"] is False
    assert state["pause_kind"] == expected_pause


def test_fixed_runner_exposes_only_safe_json_and_integer_vendor_code() -> None:
    import vendor_control_agent as agent_module

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            '{"safe_code":"VENDOR_ERROR","status":"error","vendor_code":1010}\n',
            "ip check failed from carrier formal-key-private",
        )

    result = agent_module.FixedWrapperRunner(command_runner=run).run("activate")

    assert result.returncode == 1
    assert result.safe_code == "VENDOR_ERROR"
    assert result.body == {"vendor_code": 1010}
    assert "carrier" not in repr(result)


def test_agent_rejects_pause_kind_mismatch_without_running_wrapper() -> None:
    from vendor_control_protocol import ControlRequest

    runner = FakeRunner()
    agent = _agent(runner=runner)
    agent._pause_kind = "critical"

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000011",
            "resume",
            {"pause_kind": "manual"},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "PAUSE_KIND_MISMATCH"
    assert runner.calls == []


def test_successful_manual_pause_immediately_projects_blocked_mode() -> None:
    from vendor_control_protocol import ControlRequest

    runner = FakeRunner()
    agent = _agent(runner=runner)
    agent._mode = "controlled"

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000013",
            "pause",
            {"pause_kind": "manual"},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "ok"
    assert agent._mode == "blocked"
    assert agent._pause_kind == "manual"


def test_successful_manual_pause_persists_blocked_state_before_response(
    tmp_path: Path,
) -> None:
    import json

    import vendor_control_agent as agent_module

    from vendor_control_protocol import ControlRequest

    runner = FakeRunner(
        body={
            "mode": "blocked",
            "pause_kind": "manual",
            "active_recipient_count": 1,
        }
    )
    agent = agent_module.VendorControlAgent(
        runner=runner,
        credential_store=FakeCredentialStore(configured=True),
        seal_sessions=FakeSealSessions(),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        state_path=tmp_path / "control-state.json",
        state_expected_uid=os.getuid(),
    )

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000014",
            "pause",
            {"pause_kind": "manual"},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    state = json.loads(agent.state_path.read_text(encoding="utf-8"))
    assert response.status == "ok"
    assert state["mode"] == "blocked"
    assert state["pause_kind"] == "manual"
    assert runner.calls == ["pause", "status"]


def test_vendor_1010_blocks_agent_state_and_exposes_no_vendor_message() -> None:
    from vendor_control_protocol import ControlRequest

    runner = FakeRunner(
        1,
        safe_code="VENDOR_ERROR",
        body={"vendor_code": 1010},
    )
    agent = _agent(runner=runner)

    response = agent.handle(
        ControlRequest(
            "c0a80101-0000-4000-8000-000000000012",
            "activate",
            {},
        ),
        peer_uid=os.getuid(),
        peer_gid=os.getgid(),
    )

    assert response.status == "error"
    assert response.safe_code == "VENDOR_ERROR"
    assert response.body == {"vendor_code": 1010}
    assert agent._mode == "blocked"
    assert agent._pause_kind == "critical"


def test_socket_permissions_are_fixed_and_not_caller_controlled(tmp_path: Path) -> None:
    import vendor_control_agent as agent_module

    socket_path = tmp_path / "vendor-control.sock"
    socket_path.touch(mode=0o600)

    agent_module.secure_socket(
        socket_path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    metadata = socket_path.stat()
    assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
    assert stat.S_IMODE(metadata.st_mode) == 0o660


def test_agent_uses_dedicated_socket_directory_with_backend_traverse_access(
    tmp_path: Path,
) -> None:
    import vendor_control_agent as agent_module

    socket_dir = tmp_path / "vendor-control"
    socket_path = socket_dir / "vendor-control.sock"

    agent_module.prepare_socket_directory(
        socket_path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    metadata = socket_dir.stat()
    assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
    assert stat.S_IMODE(metadata.st_mode) == 0o750
    assert Path(
        "/run/sms-platform/vendor-control/vendor-control.sock"
    ) == agent_module.CONTROL_SOCKET


def test_agent_source_has_no_arbitrary_shell_path_argv_or_environment_passthrough() -> None:
    source = (ROOT / "deploy/scripts/vendor_control_agent.py").read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "os.system" not in source
    assert "create_subprocess_shell" not in source
    assert 'body["path"]' not in source
    assert 'body["argv"]' not in source
    assert 'body["env"]' not in source
    assert "AF_INET" not in source and "start_server(" not in source
