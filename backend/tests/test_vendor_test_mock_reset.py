from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "vendor_test_mock_reset.py"
SECRET_FIXTURE = "formal-vendor-private-value-never-log"
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))


def load_module() -> ModuleType:
    assert SCRIPT.is_file(), "vendor test Mock reset manager is not implemented"
    spec = importlib.util.spec_from_file_location("vendor_test_mock_reset", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> ModuleType:
    return load_module()


def _write_live_dotenv(module: ModuleType, path: Path, *extra: str) -> None:
    vendor_origin = "https://carrier.example.com"
    values = {
        **module._LIVE_FIXED_DOTENV_VALUES,
        "VENDOR_BASE_URL": vendor_origin,
        "VENDOR_LIVE_TEST_ORIGIN": vendor_origin,
    }
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join([*lines, *extra]) + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_reset_wrapper_is_executable() -> None:
    assert stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755


def test_restore_dotenv_changes_only_fixed_live_keys(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    _write_live_dotenv(
        module,
        path,
        "SMS_API_IMAGE=registry/api@sha256:abc",
        "POSTGRES_DB=sms",
    )

    assert module.restore_pure_mock_dotenv(path) is True

    rendered = path.read_text(encoding="utf-8")
    for key, value in module.PURE_MOCK_DOTENV_VALUES.items():
        assert f"{key}={value}\n" in rendered
    for key in module._LIVE_ONLY_DOTENV_KEYS:
        assert f"{key}=" not in rendered
    assert "SMS_API_IMAGE=registry/api@sha256:abc\n" in rendered
    assert "POSTGRES_DB=sms\n" in rendered
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    module.require_restored_pure_mock_dotenv(path)


def test_live_vendor_origin_is_bound_to_dotenv_not_import_environment(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    _write_live_dotenv(module, path)

    assert module.read_live_vendor_origin(path) == "https://carrier.example.com"


def test_restore_dotenv_is_byte_stable_when_already_mock(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    lines = [
        f"{key}={value}" for key, value in module.PURE_MOCK_DOTENV_VALUES.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()

    assert module.restore_pure_mock_dotenv(path) is False
    assert path.read_bytes() == before


def test_mock_replay_retries_parent_directory_fsync(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".env"
    lines = [
        f"{key}={value}" for key, value in module.PURE_MOCK_DOTENV_VALUES.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    fsynced: list[Path] = []
    monkeypatch.setattr(module, "_fsync_parent", fsynced.append)

    assert module.restore_pure_mock_dotenv(path) is False
    assert fsynced == [path]


@pytest.mark.parametrize(
    "key,value",
    [
        ("VENDOR_BASE_URL", "https://unexpected.example.invalid"),
        ("VENDOR_LIVE_TEST_ORIGIN", "https://unexpected.example.invalid"),
        ("SMS_VENDOR_TEST_STATE_DIR", "/tmp/unexpected"),
        ("VENDOR_SECRET_KEY", "must-not-be-in-dotenv"),
    ],
)
def test_restore_dotenv_rejects_drift_without_write(
    module: ModuleType,
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    path = tmp_path / ".env"
    _write_live_dotenv(module, path)
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{key}=")
    ]
    path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()

    with pytest.raises(module.VendorTestFileError):
        module.restore_pure_mock_dotenv(path)

    assert path.read_bytes() == before


def test_remove_live_marker_is_validated_and_idempotent(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    import vendor_test_files

    path = tmp_path / "test-environment"
    vendor_test_files.write_vendor_test_marker(path, expected_uid=os.geteuid())

    assert module.remove_vendor_test_marker(path) is True
    assert not path.exists()
    assert module.remove_vendor_test_marker(path) is False

    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(module.VendorTestFileError):
        module.remove_vendor_test_marker(path)
    assert path.exists()


def test_marker_origin_must_match_the_live_dotenv_origin(
    module: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "test-environment"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "development-vendor-live",
                "vendor_origin": "https://carrier.example.com",
                "daily_segment_limit": 100,
                "timezone": "Asia/Shanghai",
                "backup_config": "/etc/sms-platform/test-update-backup.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert (
        module.read_vendor_test_marker(
            path,
            expected_vendor_origin="https://carrier.example.com",
        ).vendor_origin
        == "https://carrier.example.com"
    )
    with pytest.raises(module.VendorTestFileError):
        module.read_vendor_test_marker(
            path,
            expected_vendor_origin="https://other.example.com",
        )


class FakeOperations:
    def __init__(
        self,
        *,
        pure_mock: bool = False,
        runtime_revoked: bool = False,
        mock_ready: bool = False,
        backend_mock: bool = False,
        web_ready: bool = False,
        readers_revoked: bool = False,
        only_current: bool = False,
        store_empty: bool = False,
        marker_absent: bool = False,
        uncertain: int = 0,
        fail_once_at: str | None = None,
    ) -> None:
        self.pure_mock = pure_mock
        self.runtime_revoked = runtime_revoked
        self.mock_ready = mock_ready
        self.backend_mock = backend_mock
        self.web_ready = web_ready
        self.readers_revoked = readers_revoked
        self.only_current = only_current
        self.store_empty = store_empty
        self.marker_absent = marker_absent
        self.uncertain = uncertain
        self.fail_once_at = fail_once_at
        self.failed = False
        self.old_services_stopped = False
        self.events: list[str] = []

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_once_at == name and not self.failed:
            self.failed = True
            raise RuntimeError(f"{SECRET_FIXTURE}:{name}")

    def require_lifecycle_lock(self) -> None:
        self._event("lock")

    def dotenv_is_pure_mock(self) -> bool:
        self._event("probe_dotenv")
        return self.pure_mock

    def require_restorable_configuration(self) -> None:
        self._event("preflight_configuration")

    def stop_old_setting_services(self) -> None:
        self._event("stop_old_services")
        self.old_services_stopped = True
        self.backend_mock = False
        self.readers_revoked = False

    def restore_pure_mock_dotenv(self) -> None:
        self._event("restore_dotenv")
        assert self.old_services_stopped is True
        self.pure_mock = True

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
        assert self.old_services_stopped is True
        self.runtime_revoked = True
        self.only_current = False

    def validate_compose(self) -> None:
        self._event("compose_config")

    def start_mock_vendor(self) -> None:
        self._event("start_mock")
        assert self.pure_mock and self.runtime_revoked
        self.mock_ready = True

    def mock_vendor_is_ready(self) -> bool:
        self._event("probe_mock")
        return self.mock_ready

    def start_backend_services(self) -> None:
        self._event("start_backend")
        assert self.mock_ready and self.pure_mock and self.runtime_revoked
        self.backend_mock = True
        self.web_ready = True
        self.readers_revoked = True

    def backend_services_are_mock(self) -> bool:
        self._event("probe_backend")
        return self.backend_mock

    def web_reaches_api(self) -> bool:
        self._event("probe_web")
        return self.web_ready

    def restore_recovery_surface(self) -> None:
        self._event("restore_recovery_surface")

    def credential_store_is_empty(self) -> bool:
        self._event("probe_store")
        return self.store_empty

    def reset_credential_store(self) -> None:
        self._event("reset_store")
        assert self.backend_mock and self.readers_revoked
        self.store_empty = True

    def cleanup_stale(self) -> None:
        self._event("cleanup_stale")
        assert self.store_empty is True
        self.only_current = True

    def live_marker_is_absent(self) -> bool:
        self._event("probe_marker")
        return self.marker_absent

    def remove_live_marker(self) -> None:
        self._event("remove_marker")
        assert self.store_empty and self.only_current
        assert self.backend_mock and self.readers_revoked and self.mock_ready
        self.marker_absent = True


def test_live_reset_stops_then_converges_to_verified_mock(
    module: ModuleType,
) -> None:
    operations = FakeOperations(uncertain=7)

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    ordered = [
        "preflight_configuration",
        "stop_old_services",
        "restore_dotenv",
        "revoke_runtime",
        "start_mock",
        "start_backend",
        "reset_store",
        "cleanup_stale",
        "remove_marker",
    ]
    assert [operations.events.index(name) for name in ordered] == sorted(
        operations.events.index(name) for name in ordered
    )
    assert operations.uncertain == 7
    assert operations.pure_mock is True
    assert operations.runtime_revoked is True
    assert operations.backend_mock is True
    assert operations.web_ready is True
    assert operations.readers_revoked is True
    assert operations.store_empty is True
    assert operations.only_current is True
    assert operations.marker_absent is True


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


def test_actual_vendor_origin_preflight_precedes_stop_on_legal_path(
    module: ModuleType,
) -> None:
    class ActualOriginOperations(FakeOperations):
        def require_restorable_configuration(self) -> None:
            self._event("preflight:https://carrier.example.com")

    operations = ActualOriginOperations()

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    assert operations.events.index(
        "preflight:https://carrier.example.com"
    ) < operations.events.index("stop_old_services")


def test_actual_vendor_origin_preflight_failure_never_calls_stop(
    module: ModuleType,
) -> None:
    class InvalidActualOriginOperations(FakeOperations):
        def require_restorable_configuration(self) -> None:
            self._event("preflight:https://carrier.example.com")
            raise module.VendorTestFileError("marker mismatch")

    operations = InvalidActualOriginOperations()

    with pytest.raises(module.VendorRuntimeResetError, match="runtime reset failed"):
        module.VendorRuntimeResetManager(operations).reset()

    assert "preflight:https://carrier.example.com" in operations.events
    assert "stop_old_services" not in operations.events
    assert "restore_recovery_surface" not in operations.events


def test_reset_is_probe_only_when_runtime_readers_and_cleanup_are_complete(
    module: ModuleType,
) -> None:
    operations = FakeOperations(
        pure_mock=True,
        runtime_revoked=True,
        mock_ready=True,
        backend_mock=True,
        web_ready=True,
        readers_revoked=True,
        only_current=True,
        store_empty=True,
        marker_absent=True,
    )

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    for mutation in (
        "stop_old_services",
        "restore_dotenv",
        "revoke_runtime",
        "start_mock",
        "start_backend",
        "reset_store",
        "cleanup_stale",
        "remove_marker",
    ):
        assert mutation not in operations.events


def test_existing_backlog_does_not_block_mock_cutover(module: ModuleType) -> None:
    class BacklogOperations(FakeOperations):
        legacy_backlog = {
            "queued": 2,
            "scheduled": 3,
            "pending": 5,
            "submitting": 7,
            "retrying": 11,
        }

    operations = BacklogOperations(uncertain=13)

    result = module.VendorRuntimeResetManager(operations).reset()

    assert result.status == "runtime_revoked"
    assert operations.old_services_stopped is True
    assert operations.pure_mock is True
    assert operations.runtime_revoked is True
    assert operations.uncertain == 13


def test_safe_partial_replay_deletes_store_then_stale_then_marker(
    module: ModuleType,
) -> None:
    operations = FakeOperations(
        pure_mock=True,
        runtime_revoked=True,
        mock_ready=True,
        backend_mock=True,
        web_ready=True,
        readers_revoked=True,
    )

    module.VendorRuntimeResetManager(operations).reset()

    assert operations.events.index("reset_store") < operations.events.index(
        "cleanup_stale"
    )
    assert operations.events.index("cleanup_stale") < operations.events.index(
        "remove_marker"
    )
    assert operations.events[-1] == "probe_marker"
    assert "stop_old_services" not in operations.events


@pytest.mark.parametrize(
    "failure",
    [
        "stop_old_services",
        "restore_dotenv",
        "revoke_runtime",
        "compose_config",
        "start_mock",
        "start_backend",
        "reset_store",
        "cleanup_stale",
        "remove_marker",
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
    assert operations.store_empty is True
    assert operations.marker_absent is True


def test_backend_start_failure_never_restarts_old_live_consumers(
    module: ModuleType,
) -> None:
    operations = FakeOperations(fail_once_at="start_backend")
    manager = module.VendorRuntimeResetManager(operations)

    with pytest.raises(module.VendorRuntimeResetError):
        manager.reset()

    assert operations.runtime_revoked is True
    assert operations.readers_revoked is False
    assert operations.old_services_stopped is True
    assert operations.events.index("start_mock") < operations.events.index("start_backend")
    assert "restore_recovery_surface" in operations.events

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
        pure_mock=True,
        runtime_revoked=True,
        mock_ready=True,
        backend_mock=True,
        web_ready=True,
        readers_revoked=True,
        only_current=True,
        store_empty=True,
        marker_absent=True,
    )
    monkeypatch.setattr(module, "HostRuntimeResetOperations", lambda **_kwargs: operations)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "PLATFORM_ROOT", root)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("SMS_SECRETS_MODE", "development")
    monkeypatch.setenv("SMS_PLATFORM_ROOT", str(root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime))

    result = module.main(["__locked", "vendor-test", "reset-to-mock"])

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
    monkeypatch.setattr(module, "PLATFORM_ROOT", root)
    monkeypatch.setattr(module, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("SMS_SECRETS_MODE", "development")
    monkeypatch.setenv("SMS_PLATFORM_ROOT", str(root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime))

    result = module.main(["__locked", "vendor-test", "reset-to-mock"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "vendor runtime reset failed\n"
    assert SECRET_FIXTURE not in captured.err


def test_cli_rejects_noncanonical_platform_root(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SMS_SECRETS_MODE", "development")
    monkeypatch.setenv("SMS_PLATFORM_ROOT", "/tmp/not-the-platform")

    result = module.main(["__locked", "vendor-test", "reset-to-mock"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "vendor runtime reset failed\n"


def test_host_operations_use_fixed_services_paths_and_sanitized_compose_env(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.commands: list[tuple[list[str], dict[str, str] | None]] = []

        def succeeds(
            self,
            command: list[str] | tuple[str, ...],
            *,
            env: dict[str, str] | None = None,
        ) -> bool:
            self.commands.append((list(command), env))
            return True

        def output(
            self,
            command: list[str] | tuple[str, ...],
            *,
            env: dict[str, str] | None = None,
        ) -> bytes:
            self.commands.append((list(command), env))
            assert "ps" in command
            return b""

    class FakeStore:
        def __init__(self, path: Path) -> None:
            assert path == Path("/var/lib/sms-platform/vendor-test/credentials")
            self.empty = False

        def reset_required(self) -> bool:
            return not self.empty

        def reset(self) -> SimpleNamespace:
            self.empty = True
            return SimpleNamespace(configured=False, state="setup_required")

    monkeypatch.setattr(module, "VendorCredentialStore", FakeStore)
    monkeypatch.setenv("SMS_VENDOR_TEST_STATE_DIR", "/var/lib/live-state")
    monkeypatch.setenv("SMS_VENDOR_CONTROL_SOCKET_DIR", "/run/live-control")
    root = tmp_path / "platform"
    runtime = tmp_path / "runtime"
    runner = RecordingRunner()
    operations = module.HostRuntimeResetOperations(
        root=root,
        runtime_root=runtime,
        runner=runner,
    )

    operations.stop_old_setting_services()
    operations.revoke_runtime()
    operations.validate_compose()
    operations.start_mock_vendor()
    assert operations.mock_vendor_is_ready() is True
    operations.start_backend_services()
    assert operations.backend_services_are_mock() is True
    assert operations.web_reaches_api() is True
    assert operations.readers_are_revoked() is True
    assert operations.credential_store_is_empty() is False
    operations.reset_credential_store()
    assert operations.credential_store_is_empty() is True
    operations.cleanup_stale()

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy/docker-compose.yml"),
    ]
    commands = [command for command, _env in runner.commands]
    assert [*compose, "stop", *module.OLD_SETTING_SERVICES] in commands
    for service in module.OLD_SETTING_SERVICES:
        assert [
            *compose,
            "ps",
            "--status",
            "running",
            "--services",
            service,
        ] in commands
    assert [*compose, "config", "--quiet"] in commands
    assert [
        *compose,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "120",
        "mock-vendor",
    ] in commands
    assert [
        *compose,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "120",
        *module.BACKEND_SERVICES,
        "web",
    ] in commands
    assert [
        *compose,
        "exec",
        "-T",
        "web",
        "wget",
        "-q",
        "--spider",
        "http://127.0.0.1:8080/livez",
    ] in commands
    compose_envs = [
        env for command, env in runner.commands if command[: len(compose)] == compose
    ]
    assert compose_envs and all(env is not None for env in compose_envs)
    for env in compose_envs:
        assert env is not None
        assert env["SMS_VENDOR_TEST_STATE_DIR"] == "/var/lib/live-state"
        assert env["SMS_VENDOR_CONTROL_SOCKET_DIR"] == "/run/live-control"
        for removed in module._COMPOSE_DOTENV_OVERRIDE_KEYS:
            assert removed not in env
    flattened = "\n".join("|".join(command) for command in commands)
    assert "psql" not in flattened
    for forbidden in ("sudo", "sh|-c", "bash|-c", SECRET_FIXTURE):
        assert forbidden not in flattened


def test_host_stop_rejects_any_service_still_running(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningServiceRunner:
        def succeeds(
            self,
            _command: list[str] | tuple[str, ...],
            *,
            env: dict[str, str] | None = None,
        ) -> bool:
            assert env is not None
            return True

        def output(
            self,
            command: list[str] | tuple[str, ...],
            *,
            env: dict[str, str] | None = None,
        ) -> bytes:
            assert env is not None
            return b"worker-realtime\n" if command[-1] == "worker-realtime" else b""

    monkeypatch.setattr(
        module,
        "VendorCredentialStore",
        lambda _path: SimpleNamespace(reset_required=lambda: False),
    )
    operations = module.HostRuntimeResetOperations(
        root=tmp_path / "platform",
        runtime_root=tmp_path / "runtime",
        runner=RunningServiceRunner(),
    )

    with pytest.raises(module.VendorRuntimeResetError, match="runtime reset failed"):
        operations.stop_old_setting_services()


def test_failure_recovery_surface_starts_only_api_then_web(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    class RecordingRunner:
        def succeeds(
            self,
            command: list[str] | tuple[str, ...],
            *,
            env: dict[str, str] | None = None,
        ) -> bool:
            assert env is not None
            commands.append(list(command))
            return True

    monkeypatch.setattr(
        module,
        "VendorCredentialStore",
        lambda _path: SimpleNamespace(reset_required=lambda: False),
    )
    root = tmp_path / "platform"
    operations = module.HostRuntimeResetOperations(
        root=root,
        runtime_root=tmp_path / "runtime",
        runner=RecordingRunner(),
    )

    operations.restore_recovery_surface()

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy/docker-compose.yml"),
    ]
    assert commands == [
        [
            *compose,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
        ],
        [
            *compose,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        ],
        [
            *compose,
            "exec",
            "-T",
            "web",
            "wget",
            "-q",
            "--spider",
            "http://127.0.0.1:8080/livez",
        ],
    ]


def test_host_restore_binds_live_dotenv_and_marker_to_actual_vendor_origin(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_origin = "https://carrier.example.com"
    observed: list[tuple[str, object]] = []

    monkeypatch.setattr(
        module,
        "VendorCredentialStore",
        lambda _path: SimpleNamespace(reset_required=lambda: False),
    )
    monkeypatch.setattr(
        module,
        "require_restored_pure_mock_dotenv",
        lambda _path: (_ for _ in ()).throw(module.VendorTestFileError("live")),
    )
    monkeypatch.setattr(
        module,
        "read_live_vendor_origin",
        lambda _path: vendor_origin,
    )

    def read_marker(_path: Path, **kwargs: object) -> SimpleNamespace:
        observed.append(("marker", kwargs["expected_vendor_origin"]))
        return SimpleNamespace(vendor_origin=vendor_origin)

    monkeypatch.setattr(module, "read_vendor_test_marker", read_marker)
    monkeypatch.setattr(
        module,
        "restore_pure_mock_dotenv",
        lambda _path: observed.append(("restore", vendor_origin)),
    )

    def remove_marker(_path: Path, **kwargs: object) -> None:
        observed.append(("remove", kwargs["expected_vendor_origin"]))

    monkeypatch.setattr(module, "remove_vendor_test_marker", remove_marker)
    operations = module.HostRuntimeResetOperations(
        root=tmp_path / "platform",
        runtime_root=tmp_path / "runtime",
        runner=SimpleNamespace(),
    )

    operations.require_restorable_configuration()
    operations.restore_pure_mock_dotenv()
    operations.remove_live_marker()

    assert observed == [
        ("marker", vendor_origin),
        ("marker", vendor_origin),
        ("restore", vendor_origin),
        ("remove", vendor_origin),
    ]
