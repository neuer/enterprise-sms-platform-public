from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "vendor_runtime_reset.py"
SECRET_FIXTURE = "formal-vendor-private-value-never-log"
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))


def load_module() -> ModuleType:
    assert SCRIPT.is_file(), "vendor runtime reset manager is not implemented"
    spec = importlib.util.spec_from_file_location("vendor_runtime_reset", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> ModuleType:
    return load_module()


class FakeOperations:
    def __init__(
        self,
        *,
        runtime_revoked: bool = False,
        readers_revoked: bool = False,
        only_current: bool = False,
        fail_once_at: str | None = None,
    ) -> None:
        self.runtime_revoked = runtime_revoked
        self.readers_revoked = readers_revoked
        self.only_current = only_current
        self.fail_once_at = fail_once_at
        self.failed = False
        self.events: list[str] = []

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_once_at == name and not self.failed:
            self.failed = True
            raise RuntimeError(f"{SECRET_FIXTURE}:{name}")

    def require_lifecycle_lock(self) -> None:
        self._event("lock")

    def runtime_is_revoked(self) -> bool:
        self._event("probe_runtime")
        return self.runtime_revoked

    def readers_are_revoked(self) -> bool:
        self._event("probe_readers")
        return self.readers_revoked

    def only_current_generation(self) -> bool:
        self._event("probe_stale")
        return self.only_current

    def revoke_runtime(self) -> None:
        self._event("revoke_runtime")
        self.runtime_revoked = True
        self.only_current = False

    def validate_compose(self) -> None:
        self._event("compose_config")

    def stop_readers(self) -> None:
        self._event("stop_readers")
        self.readers_revoked = False

    def remove_readers(self) -> None:
        self._event("remove_readers")
        self.readers_revoked = False

    def start_readers(self) -> None:
        self._event("start_readers")
        self.readers_revoked = True

    def cleanup_stale(self) -> None:
        self._event("cleanup_stale")
        self.only_current = True


def test_reset_replaces_all_readers_before_cleaning_stale_generations(
    module: ModuleType,
) -> None:
    operations = FakeOperations()

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    assert operations.events == [
        "lock",
        "probe_runtime",
        "revoke_runtime",
        "compose_config",
        "stop_readers",
        "remove_readers",
        "start_readers",
        "probe_readers",
        "cleanup_stale",
        "probe_runtime",
        "probe_readers",
        "probe_stale",
    ]


def test_reset_never_reads_existing_container_vendor_files_before_runtime_revocation(
    module: ModuleType,
) -> None:
    class GuardedOperations(FakeOperations):
        def readers_are_revoked(self) -> bool:
            if not self.runtime_revoked:
                raise AssertionError("old container vendor files must not be read")
            return super().readers_are_revoked()

    operations = GuardedOperations()

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"


def test_reset_is_probe_only_when_runtime_readers_and_cleanup_are_complete(
    module: ModuleType,
) -> None:
    operations = FakeOperations(
        runtime_revoked=True,
        readers_revoked=True,
        only_current=True,
    )

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    assert operations.events == [
        "lock",
        "probe_runtime",
        "probe_readers",
        "probe_stale",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "revoke_runtime",
        "compose_config",
        "stop_readers",
        "remove_readers",
        "start_readers",
        "cleanup_stale",
    ],
)
def test_same_operation_replay_converges_after_each_partial_failure(
    module: ModuleType,
    failure: str,
) -> None:
    operations = FakeOperations(fail_once_at=failure)
    manager = module.VendorRuntimeResetManager(operations)

    with pytest.raises(module.VendorRuntimeResetError, match="runtime reset failed") as exc:
        manager.reset()
    assert SECRET_FIXTURE not in str(exc.value)

    result = manager.reset()

    assert result.status == "runtime_revoked"
    assert operations.runtime_revoked is True
    assert operations.readers_revoked is True
    assert operations.only_current is True


def test_up_failure_after_remove_never_marks_old_readers_revoked(
    module: ModuleType,
) -> None:
    operations = FakeOperations(fail_once_at="start_readers")
    manager = module.VendorRuntimeResetManager(operations)

    with pytest.raises(module.VendorRuntimeResetError):
        manager.reset()

    assert operations.runtime_revoked is True
    assert operations.readers_revoked is False
    assert operations.events.index("remove_readers") < operations.events.index(
        "start_readers"
    )

    manager.reset()
    assert operations.readers_revoked is True


def test_cli_emits_only_exact_safe_success_document(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "platform"
    runtime = tmp_path / "runtime"
    root.mkdir()
    runtime.mkdir()
    operations = FakeOperations(
        runtime_revoked=True,
        readers_revoked=True,
        only_current=True,
    )
    monkeypatch.setattr(module, "HostRuntimeResetOperations", lambda **_kwargs: operations)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SMS_SECRETS_MODE", "development")

    result = module.main(
        [
            "--root",
            str(root),
            "--runtime-root",
            str(runtime),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"status": "runtime_revoked"}


def test_cli_failure_suppresses_private_exception_text(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "platform"
    runtime = tmp_path / "runtime"
    root.mkdir()
    runtime.mkdir()
    operations = FakeOperations(fail_once_at="revoke_runtime")
    monkeypatch.setattr(module, "HostRuntimeResetOperations", lambda **_kwargs: operations)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SMS_SECRETS_MODE", "development")

    result = module.main(
        [
            "--root",
            str(root),
            "--runtime-root",
            str(runtime),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "vendor runtime reset failed\n"
    assert SECRET_FIXTURE not in captured.err


def test_host_operations_use_only_fixed_preprocessor_and_reader_service_argv(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def succeeds(self, command: list[str] | tuple[str, ...]) -> bool:
            self.commands.append(list(command))
            return True

    root = tmp_path / "platform"
    runtime = tmp_path / "runtime"
    runner = RecordingRunner()
    operations = module.HostRuntimeResetOperations(
        root=root,
        runtime_root=runtime,
        runner=runner,
    )

    assert operations.runtime_is_revoked() is True
    assert operations.readers_are_revoked() is True
    assert operations.only_current_generation() is True
    operations.revoke_runtime()
    operations.validate_compose()
    operations.stop_readers()
    operations.remove_readers()
    operations.start_readers()
    operations.cleanup_stale()

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy/docker-compose.yml"),
    ]
    readers = ["api", "worker-realtime", "worker-bulk"]
    assert runner.commands[0][-3:] == [
        "verify-vendor-revoked",
        "--runtime-root",
        str(runtime),
    ]
    assert [command[len(compose) : len(compose) + 3] for command in runner.commands[1:4]] == [
        ["exec", "-T", service] for service in readers
    ]
    assert runner.commands[4][-3:] == [
        "verify-only-current",
        "--runtime-root",
        str(runtime),
    ]
    assert runner.commands[6] == [*compose, "config", "--quiet"]
    assert runner.commands[7] == [*compose, "stop", *readers]
    assert runner.commands[8] == [*compose, "rm", "-sf", *readers]
    assert runner.commands[9] == [
        *compose,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "120",
        *readers,
    ]
    assert runner.commands[10][-4:] == [
        "cleanup",
        "--runtime-root",
        str(runtime),
        "--stale",
    ]
    flattened = "\n".join("|".join(command) for command in runner.commands)
    for forbidden in ("sudo", "sh|-c", "bash|-c", SECRET_FIXTURE):
        assert forbidden not in flattened
