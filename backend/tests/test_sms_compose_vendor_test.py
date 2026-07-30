from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy/sms-compose"
LOCK_RUNNER = ROOT / "deploy/scripts/run_with_lifecycle_lock.py"


@pytest.fixture
def control_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    platform = tmp_path / "platform"
    scripts = platform / "deploy/scripts"
    scripts.mkdir(parents=True)
    (platform / "deploy/secrets").mkdir(mode=0o700)
    (platform / "deploy/docker-compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (platform / ".env").write_text("# fixture\n", encoding="utf-8")
    (scripts / "run_with_lifecycle_lock.py").write_text(
        LOCK_RUNNER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    log = tmp_path / "control.log"
    recorder = """from __future__ import annotations
import os
import sys
from pathlib import Path
with Path(os.environ["CONTROL_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(Path(__file__).name + "|" + "|".join(sys.argv[1:]))
    stream.write("|locked=" + os.environ.get("SMS_LIFECYCLE_LOCKED", ""))
    stream.write("|fd=" + os.environ.get("SMS_LIFECYCLE_LOCK_FD", "") + "\\n")
"""
    for name in (
        "vendor_test_bootstrap.py",
        "vendor_test_manager.py",
        "vendor_runtime_reset.py",
        "test_update_manager.py",
        "test_secure_access_manager.py",
        "prepare_runtime_secrets.py",
        "vendor_control_reload.py",
    ):
        (scripts / name).write_text(recorder, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "SMS_PLATFORM_ROOT": str(platform),
            "SMS_RUNTIME_ROOT": str(runtime),
            "SMS_SECRETS_MODE": "development",
            "SMS_SECURE_ACCESS_MANAGER_SMOKE": "1",
            "CONTROL_LOG": str(log),
        }
    )
    return environment, log


def _run(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WRAPPER), *args],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("action", ["vendor-test", "test-update", "secure-access"])
def test_production_rejects_control_planes_before_any_helper(
    control_environment: tuple[dict[str, str], Path],
    action: str,
) -> None:
    environment, log = control_environment
    environment["SMS_SECRETS_MODE"] = "production"

    result = _run(environment, action, "status")

    assert result.returncode == 2
    assert "forbidden in production" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("vendor-test", "shell"),
        ("vendor-test", "activate", "--force"),
        ("vendor-test", "reset-runtime", "--force"),
        ("vendor-test", "reload-agent", "--force"),
        ("vendor-test", "allow-recipient", "add", "13800138000"),
        ("test-update", "rollback"),
        ("test-update", "bootstrap-public-cutover"),
        ("test-update", "apply", "--compose-args", "down"),
        ("secure-access",),
        ("secure-access", "shell"),
        ("secure-access", "start", "--origin", "http://evil"),
        ("secure-access", "install"),
    ],
)
def test_control_planes_reject_arbitrary_passthrough(
    control_environment: tuple[dict[str, str], Path],
    arguments: tuple[str, ...],
) -> None:
    environment, log = control_environment

    result = _run(environment, *arguments)

    assert result.returncode == 2
    assert not log.exists()


def test_vendor_mutations_and_test_update_prepare_inherit_the_verified_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    mutations = [
        _run(environment, "vendor-test", command)
        for command in ("activate", "pause", "resume", "rotate", "recover-rotation")
    ]
    update = _run(environment, "test-update", "prepare")

    assert all(result.returncode == 0 for result in mutations)
    assert update.returncode == 0, update.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    managers = [line for line in lines if "manager.py|" in line]
    for command in ("activate", "pause", "resume", "rotate", "recover-rotation"):
        assert any(f"vendor_test_manager.py|{command}|" in line for line in managers)
    assert any("test_update_manager.py|prepare|" in line for line in managers)
    assert all("|locked=1|fd=" in line for line in managers)
    assert all(line.rsplit("|fd=", 1)[1].isdigit() for line in managers)


def test_vendor_bootstrap_is_a_fixed_development_action_under_lifecycle_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    result = _run(environment, "vendor-test", "bootstrap")

    assert result.returncode == 0, result.stderr
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("vendor_test_bootstrap.py|")
    assert "|locked=1|fd=" in line


def test_vendor_runtime_reset_is_fixed_and_inherits_the_verified_lifecycle_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    result = _run(environment, "vendor-test", "reset-runtime")

    assert result.returncode == 0, result.stderr
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("vendor_runtime_reset.py|")
    assert "|--root|" in line
    assert "|--runtime-root|" in line
    assert "|locked=1|fd=" in line


def test_vendor_agent_reload_is_zero_arg_development_action_under_verified_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    result = _run(environment, "vendor-test", "reload-agent")

    assert result.returncode == 0, result.stderr
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("vendor_control_reload.py|")
    assert "|locked=1|fd=" in line
    assert line.rsplit("|fd=", 1)[1].isdigit()


def test_vendor_agent_reload_fails_without_restart_when_lifecycle_lock_is_held(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment
    runtime_root = Path(environment["SMS_RUNTIME_ROOT"])
    lock_path = Path(f"{runtime_root}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _run(environment, "vendor-test", "reload-agent")
    finally:
        os.close(descriptor)

    assert result.returncode == 1
    assert "lifecycle lock is already held" in result.stderr
    assert not log.exists()


def test_vendor_agent_reload_is_rejected_in_production_before_helper(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment
    environment["SMS_SECRETS_MODE"] = "production"

    result = _run(environment, "vendor-test", "reload-agent")

    assert result.returncode == 2
    assert "forbidden in production" in result.stderr
    assert not log.exists()


def test_status_is_read_only_and_does_not_claim_a_lifecycle_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    result = _run(environment, "vendor-test", "status")

    assert result.returncode == 0
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("vendor_test_manager.py|status|")
    assert "|locked=|fd=" in line


def test_update_capability_is_read_only_and_does_not_claim_lifecycle_lock(
    control_environment: tuple[dict[str, str], Path],
) -> None:
    environment, log = control_environment

    result = _run(environment, "test-update", "capability")

    assert result.returncode == 0
    line = log.read_text(encoding="utf-8").strip()
    assert line.startswith("test_update_manager.py|capability|")
    assert "|locked=|fd=" in line


@pytest.mark.parametrize("command", ["start", "status", "stop"])
def test_secure_access_is_a_fixed_transport_action_without_lifecycle_or_secrets(
    control_environment: tuple[dict[str, str], Path],
    command: str,
) -> None:
    environment, log = control_environment
    environment["SMS_RELEASE_ROOT"] = "/invalid-without-smoke-pair"

    result = _run(environment, "secure-access", command)

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").strip() == (
        f"test_secure_access_manager.py|{command}|locked=|fd="
    )


def test_secure_access_usage_is_development_only_and_not_a_credential_action() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "development only: secure-access start|status|stop" in source
    assert (
        'HOST_CONTROL_ROOT="/usr/local/libexec/sms-platform/test-secure-access"'
        in source
    )
    assert 'TEST_SECURE_ACCESS_MANAGER="$HOST_CONTROL_ROOT/' in source
    assert 'TEST_UPDATE_MANAGER="$HOST_CONTROL_ROOT/test_update_manager.py"' in source
    assert 'LOCK_RUNNER="$HOST_CONTROL_ROOT/run_with_lifecycle_lock.py"' in source
    assert 'python_path="$HOST_CONTROL_ROOT"' in source
    assert "PYTHONNOUSERSITE=1" in source
    assert "LOCK_PYTHON=(" in source
    assert "/usr/bin/python3" in source
    assert '"${LOCK_PYTHON[@]}" "$LOCK_RUNNER"' in source
    secure_dispatch = source.split("dispatch_secure_access()", maxsplit=1)[1].split(
        "run_locked_operation()", maxsplit=1
    )[0]
    assert "/usr/bin/python3" in secure_dispatch
    assert "PYTHONNOUSERSITE=1" in secure_dispatch
    assert '"PYTHONPATH=$HOST_CONTROL_ROOT"' in secure_dispatch
    assert "SMS_SECURE_ACCESS_INTERNAL=1" in source
    assert "bootstrap wrapper only permits test-update" in source
    assert '[[ "$HOST_BOOTSTRAP" == 1 ]] || return 2' in source
    assert '[[ "${SMS_PUBLIC_CUTOVER_CONFIRMED:-}" == 1 ]]' in source
    assert "public_cutover_bootstrap.py" in source
    assert "verify-assets >/dev/null" in source
    assert (
        "bootstrap-public-cutover | prepare | apply | verify"
        in source
    )
    for forbidden in (
        "prepare_runtime_secrets",
        "run_with_lifecycle_lock",
        "install",
        "credentials",
        "recipient",
        "activate",
    ):
        assert forbidden not in secure_dispatch


def test_vendor_control_source_exposes_no_tty_credential_or_recipient_flow() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "install-credentials" not in source
    assert "allow-recipient" not in source
    assert (
        "vendor-test bootstrap|activate|pause|resume|rotate|recover-rotation|"
        "reset-runtime|reload-agent|status"
        in source
    )
    assert "GetReport" not in source and "GetReply" not in source
    assert stat.S_IMODE(WRAPPER.stat().st_mode) & stat.S_IXUSR


def test_forged_private_control_action_is_rejected() -> None:
    result = subprocess.run(
        [str(WRAPPER), "__locked", "vendor-test", "activate"],
        env={**os.environ, "SMS_SECRETS_MODE": "development"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "private locked action" in result.stderr
