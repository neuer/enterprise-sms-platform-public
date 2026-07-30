from __future__ import annotations

import fcntl
import os
import pty
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy" / "sms-compose"
LOCK_RUNNER = ROOT / "deploy" / "scripts" / "run_with_lifecycle_lock.py"


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_environment(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    platform_root = tmp_path / "platform"
    (platform_root / "deploy" / "scripts").mkdir(parents=True)
    (platform_root / "deploy" / "secrets").mkdir()
    (platform_root / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    preprocessor = platform_root / "deploy" / "scripts" / "prepare_runtime_secrets.py"
    preprocessor.write_text(
        """from __future__ import annotations

import os
import sys
from pathlib import Path

arguments = ["python3", __file__, *sys.argv[1:]]
with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write("|".join(arguments) + "\\n")

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "prepare":
    runtime = Path(sys.argv[sys.argv.index("--runtime-root") + 1])
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
elif command == "current-target":
    print(
        os.environ.get(
            "FAKE_CURRENT_TARGET",
            "generations/generation-00000000000000000000000000000000",
        )
    )
elif command == "activate":
    raise SystemExit(int(os.environ.get("FAKE_ACTIVATE_EXIT", "0")))
raise SystemExit(int(os.environ.get("FAKE_PYTHON_EXIT", "0")))
""",
        encoding="utf-8",
    )
    if LOCK_RUNNER.is_file():
        (platform_root / "deploy" / "scripts" / LOCK_RUNNER.name).write_text(
            LOCK_RUNNER.read_text(encoding="utf-8"), encoding="utf-8"
        )
    release_manager = platform_root / "deploy" / "scripts" / "release_manager.py"
    release_manager.write_text(
        """from __future__ import annotations

import os
import sys
import time
from pathlib import Path

arguments = ["python3", __file__, *sys.argv[1:]]
with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write("|".join(arguments) + "\\n")

if os.environ.get("FAKE_RELEASE_ENTERED_FILE"):
    Path(os.environ["FAKE_RELEASE_ENTERED_FILE"]).touch()
    while not Path(os.environ["FAKE_RELEASE_RELEASE_FILE"]).exists():
        time.sleep(0.05)
raise SystemExit(int(os.environ.get("FAKE_RELEASE_EXIT", "0")))
""",
        encoding="utf-8",
    )
    vendor_runtime_reset = (
        platform_root / "deploy" / "scripts" / "vendor_runtime_reset.py"
    )
    vendor_runtime_reset.write_text(
        """from __future__ import annotations

import os
import sys
import time
from pathlib import Path

arguments = ["python3", __file__, *sys.argv[1:]]
with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write("|".join(arguments) + "\\n")
if os.environ.get("FAKE_RESET_ENTERED_FILE"):
    Path(os.environ["FAKE_RESET_ENTERED_FILE"]).touch()
    while not Path(os.environ["FAKE_RESET_RELEASE_FILE"]).exists():
        time.sleep(0.05)
raise SystemExit(int(os.environ.get("FAKE_RESET_EXIT", "0")))
""",
        encoding="utf-8",
    )
    (platform_root / ".env").write_text("# non-secret fixture\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    write_executable(
        fake_bin / "flock",
        """#!/bin/sh
printf 'external-flock-was-called\n' >> "$COMMAND_LOG"
exit 97
""",
    )
    write_executable(
        fake_bin / "docker",
        """#!/bin/sh
{
  printf 'docker'
  for arg in "$@"; do printf '|%s' "$arg"; done
  printf '|runtime=%s\n' "${SMS_RUNTIME_SECRETS_DIR:-unset}"
} >> "$COMMAND_LOG"
case " $* " in
  *" config --quiet "*) exit "${FAKE_CONFIG_EXIT:-0}" ;;
  *" down "*) exit "${FAKE_DOWN_EXIT:-0}" ;;
  *" ps "*)
    printf '%s' "${FAKE_PS_OUTPUT:-}"
    exit "${FAKE_PS_EXIT:-0}"
    ;;
  *" up "*)
    up_count="$(grep -c '|up|' "$COMMAND_LOG")"
    if [ "$up_count" -le "${FAKE_UP_FAIL_THROUGH:-0}" ]; then
      exit "${FAKE_UP_EXIT:-29}"
    fi
    if [ -n "${FAKE_UP_ENTERED_FILE:-}" ]; then
      : > "$FAKE_UP_ENTERED_FILE"
      while [ ! -e "$FAKE_UP_RELEASE_FILE" ]; do sleep 0.05; done
    fi
    ;;
esac
exit 0
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "COMMAND_LOG": str(log),
            "SMS_PLATFORM_ROOT": str(platform_root),
            "SMS_RUNTIME_ROOT": str(runtime),
            "SMS_SECRETS_MODE": "development",
        }
    )
    for name in (
        "ENVIRONMENT",
        "DEBUG",
        "AUTH_MOCK",
        "VENDOR_MOCK",
        "COMPOSE_PROFILES",
        "SMS_RELEASE_ROOT",
        "SMS_RELEASE_SMOKE",
        "SMS_INGRESS_SUBNET",
        "SMS_API_INGRESS_IPV4",
        "SMS_WEB_INGRESS_IPV4",
    ):
        environment.pop(name, None)
    return platform_root, log, environment


def run_wrapper(
    fake_environment: tuple[Path, Path, dict[str, str]],
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not WRAPPER.is_file():
        pytest.skip("mandatory Compose wrapper is not implemented")
    _, _, environment = fake_environment
    environment = environment.copy()
    environment.update(extra_environment or {})
    return subprocess.run(
        [str(WRAPPER), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def run_wrapper_with_tty(
    fake_environment: tuple[Path, Path, dict[str, str]],
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> int:
    _, _, environment = fake_environment
    environment = environment.copy()
    environment.update(extra_environment or {})
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [str(WRAPPER), *arguments],
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        while True:
            try:
                if not os.read(master, 4096):
                    break
            except OSError:
                break
        return process.wait(timeout=10)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)


def start_lock_helper(
    fake_environment: tuple[Path, Path, dict[str, str]],
    operation: str,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    _, _, environment = fake_environment
    environment = environment.copy()
    environment.update(extra_environment or {})
    return subprocess.Popen(
        [
            sys.executable,
            str(LOCK_RUNNER),
            "--runtime-root",
            environment["SMS_RUNTIME_ROOT"],
            "--wrapper",
            str(WRAPPER),
            "--operation",
            operation,
            "--",
            *arguments,
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def command_lines(log: Path) -> list[str]:
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def compose_prefix(platform_root: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(platform_root / ".env"),
        "-f",
        str(platform_root / "deploy" / "docker-compose.yml"),
    ]


def expected_line(arguments: list[str], *, runtime: Path | None = None) -> str:
    line = "|".join(arguments)
    if runtime is not None:
        line += f"|runtime={runtime / 'current'}"
    return line


def expected_prepare(platform_root: Path, runtime: Path, *, mode: str = "development") -> str:
    if mode == "development":
        return expected_revoke_vendor(platform_root, runtime)
    return expected_line(
        [
            "python3",
            str(platform_root / "deploy" / "scripts" / "prepare_runtime_secrets.py"),
            "prepare",
            "--source-dir",
            str(platform_root / "deploy" / "secrets"),
            "--runtime-root",
            str(runtime),
            "--mode",
            mode,
        ]
    )


def expected_revoke_vendor(platform_root: Path, runtime: Path) -> str:
    return expected_line(
        [
            "python3",
            str(platform_root / "deploy" / "scripts" / "prepare_runtime_secrets.py"),
            "revoke-vendor",
            "--source-dir",
            str(platform_root / "deploy" / "secrets"),
            "--runtime-root",
            str(runtime),
            "--mode",
            "development",
        ]
    )


def expected_preprocessor(platform_root: Path, runtime: Path, command: str, *arguments: str) -> str:
    return expected_line(
        [
            "python3",
            str(platform_root / "deploy" / "scripts" / "prepare_runtime_secrets.py"),
            command,
            "--runtime-root",
            str(runtime),
            *arguments,
        ]
    )


def expected_release_manager(
    platform_root: Path,
    mode: str,
    *arguments: str,
    release_root: str = "/var/lib/sms-platform/releases",
) -> str:
    return expected_line(
        [
            "python3",
            str(platform_root / "deploy" / "scripts" / "release_manager.py"),
            "--root",
            str(platform_root),
            "--release-root",
            release_root,
            "--mode",
            mode,
            *arguments,
        ]
    )


def write_non_secret_env(
    platform_root: Path,
    *,
    environment: str = "production",
    debug: str = "0",
    auth_mock: str = "0",
    vendor_mock: str = "0",
    compose_profiles: str | None = None,
) -> None:
    lines = [
        f"ENVIRONMENT={environment}",
        f"DEBUG={debug}",
        f"AUTH_MOCK={auth_mock}",
        f"VENDOR_MOCK={vendor_mock}",
    ]
    if compose_profiles is not None:
        lines.append(f"COMPOSE_PROFILES={compose_profiles}")
    (platform_root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_sms_compose_wrapper_exists_and_is_executable() -> None:
    assert WRAPPER.is_file(), "mandatory Compose wrapper is not implemented"
    assert os.access(WRAPPER, os.X_OK), "mandatory Compose wrapper is not executable"


def test_symlink_entry_resolves_platform_root_without_environment(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    installed_wrapper = platform_root / "deploy" / "sms-compose"
    write_executable(installed_wrapper, WRAPPER.read_text(encoding="utf-8"))

    command_dir = tmp_path / "usr" / "local" / "sbin"
    command_dir.mkdir(parents=True)
    command_link = command_dir / "sms-compose"
    command_link.symlink_to(os.path.relpath(installed_wrapper, command_dir))

    environment = environment.copy()
    environment.pop("SMS_PLATFORM_ROOT")
    result = subprocess.run(
        [str(command_link), "ps"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_line(
            compose_prefix(platform_root) + ["ps"],
            runtime=Path(environment["SMS_RUNTIME_ROOT"]),
        )
    ]


def test_usage_lists_only_explicitly_allowed_actions() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "<compose-command>" not in source
    assert "config|ps|logs|exec" in source
    assert "run --rm migrate" in source
    assert "partition-maintenance [--dry-run]" in source
    assert "production up options" in source
    assert "production up services" in source
    assert "command -v flock" not in source
    assert "run_with_lifecycle_lock.py" in source
    assert "release prepare|activate|status|resume|rollback" in source
    assert "init-admin --show-temporary-password" in source


def test_init_admin_requires_tty_display_flag_and_rejects_password_arguments(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    no_flag = run_wrapper(fake_environment, "init-admin")
    no_tty = run_wrapper(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
    )
    password = run_wrapper(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
        "--password",
        "NeverAccepted@123",
    )
    unknown = run_wrapper(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
        "--unknown",
    )

    assert {no_flag.returncode, no_tty.returncode, password.returncode, unknown.returncode} == {2}
    assert command_lines(log) == []


def test_init_admin_tty_prepares_secrets_and_runs_exact_api_cli(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    returncode = run_wrapper_with_tty(
        fake_environment,
        "init-admin",
        "--username",
        "root.admin",
        "--display-name",
        "平台管理员",
        "--show-temporary-password",
    )

    assert returncode == 0
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime),
        expected_line(
            [*compose_prefix(platform_root), "config", "--quiet"],
            runtime=runtime,
        ),
        expected_line(
            [
                *compose_prefix(platform_root),
                "ps",
                "--status",
                "running",
                "-q",
                "api",
            ],
            runtime=runtime,
        ),
        expected_line(
            [
                *compose_prefix(platform_root),
                "run",
                "--rm",
                "api",
                "python",
                "-m",
                "app.cli",
                "init-admin",
                "--username",
                "root.admin",
                "--display-name",
                "平台管理员",
                "--show-temporary-password",
            ],
            runtime=runtime,
        ),
    ]


def test_init_admin_tty_executes_cli_in_running_api_container(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    returncode = run_wrapper_with_tty(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
        extra_environment={"FAKE_PS_OUTPUT": "api-container-id\n"},
    )

    assert returncode == 0
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime),
        expected_line(
            [*compose_prefix(platform_root), "config", "--quiet"],
            runtime=runtime,
        ),
        expected_line(
            [
                *compose_prefix(platform_root),
                "ps",
                "--status",
                "running",
                "-q",
                "api",
            ],
            runtime=runtime,
        ),
        expected_line(
            [
                *compose_prefix(platform_root),
                "exec",
                "api",
                "python",
                "-m",
                "app.cli",
                "init-admin",
                "--show-temporary-password",
            ],
            runtime=runtime,
        ),
    ]


def test_init_admin_production_reuses_fail_closed_launch_validation(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, debug="1")

    returncode = run_wrapper_with_tty(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert returncode == 1
    assert command_lines(log) == []


def test_release_prepare_accepts_only_absolute_manifest_and_calls_manager(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, _ = fake_environment
    manifest = tmp_path / "staging" / "manifest.json"

    result = run_wrapper(
        fake_environment,
        "release",
        "prepare",
        "--manifest",
        str(manifest),
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_release_manager(
            platform_root,
            "development",
            "prepare",
            "--manifest",
            str(manifest),
        )
    ]


@pytest.mark.parametrize("subcommand", ["activate", "resume", "rollback"])
def test_release_mutations_prepare_runtime_secrets_then_call_locked_manager(
    fake_environment: tuple[Path, Path, dict[str, str]],
    subcommand: str,
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(
        fake_environment,
        "release",
        subcommand,
        "--release-id",
        "release-20260714",
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime),
        expected_release_manager(
            platform_root,
            "development",
            subcommand,
            "--release-id",
            "release-20260714",
        ),
    ]


def test_vendor_runtime_reset_and_release_mutations_share_one_lifecycle_lock(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    _, log, _ = fake_environment
    entered = tmp_path / "reset-entered"
    release_reset = tmp_path / "release-reset"
    reset = start_lock_helper(
        fake_environment,
        "vendor-test",
        "reset-runtime",
        extra_environment={
            "FAKE_RESET_ENTERED_FILE": str(entered),
            "FAKE_RESET_RELEASE_FILE": str(release_reset),
        },
    )
    deadline = time.monotonic() + 5
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert entered.exists()

    release = run_wrapper(
        fake_environment,
        "release",
        "activate",
        "--release-id",
        "release-20260719",
    )

    assert release.returncode != 0
    assert "lifecycle lock is already held" in release.stderr
    assert not any("release_manager.py|" in line for line in command_lines(log))
    release_reset.touch()
    stdout, stderr = reset.communicate(timeout=10)
    assert reset.returncode == 0, stderr or stdout


def test_release_status_is_read_only_and_does_not_acquire_lifecycle_lock(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    lock_path = Path(f"{environment['SMS_RUNTIME_ROOT']}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(
            fake_environment,
            "release",
            "status",
            "--release-id",
            "release-20260714",
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_release_manager(
            platform_root,
            "development",
            "status",
            "--release-id",
            "release-20260714",
        )
    ]


def test_development_smoke_dual_switch_passes_isolated_release_root_as_argv(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, _ = fake_environment
    release_root = tmp_path / "sms-platform-release-control-Ab12Cd34" / "releases"

    result = run_wrapper(
        fake_environment,
        "release",
        "status",
        "--release-id",
        "release-20260714",
        extra_environment={
            "SMS_RELEASE_SMOKE": "1",
            "SMS_RELEASE_ROOT": str(release_root),
        },
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_release_manager(
            platform_root,
            "development",
            "status",
            "--release-id",
            "release-20260714",
            release_root=str(release_root),
        )
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"SMS_RELEASE_SMOKE": "1"},
        {"SMS_RELEASE_ROOT": "/tmp/sms-platform-release-control-Ab12Cd34/releases"},
        {
            "SMS_RELEASE_SMOKE": "0",
            "SMS_RELEASE_ROOT": "/tmp/sms-platform-release-control-Ab12Cd34/releases",
        },
        {"SMS_RELEASE_SMOKE": "1", "SMS_RELEASE_ROOT": ""},
    ],
)
def test_development_release_root_override_requires_exact_dual_switch_before_tools(
    fake_environment: tuple[Path, Path, dict[str, str]],
    override: dict[str, str],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "release",
        "status",
        "--release-id",
        "release-20260714",
        extra_environment=override,
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("up", "-d"),
        ("down", "--remove-orphans"),
        ("rotate", "backend"),
        ("run", "--rm", "migrate"),
        ("release", "prepare", "--manifest", "/tmp/staging/manifest.json"),
        ("release", "activate", "--release-id", "release-20260714"),
        ("release", "status", "--release-id", "release-20260714"),
        ("release", "resume", "--release-id", "release-20260714"),
        ("release", "rollback", "--release-id", "release-20260714"),
    ],
)
@pytest.mark.parametrize("variable", ["SMS_RELEASE_ROOT", "SMS_RELEASE_SMOKE"])
@pytest.mark.parametrize("value", ["", "1"])
def test_production_change_and_release_entries_reject_smoke_variable_presence(
    fake_environment: tuple[Path, Path, dict[str, str]],
    arguments: tuple[str, ...],
    variable: str,
    value: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        *arguments,
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            variable: value,
        },
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("release",),
        ("release", "unknown"),
        ("release", "prepare", "--manifest", "relative/manifest.json"),
        ("release", "prepare", "--manifest", "/tmp/manifest.json", "extra"),
        ("release", "activate", "--release-id", "../release"),
        ("release", "status", "--release-id"),
        ("release", "resume", "--manifest", "/tmp/manifest.json"),
    ],
)
def test_release_rejects_unknown_shapes_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    arguments: tuple[str, ...],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, *arguments)

    assert result.returncode == 2
    assert command_lines(log) == []


@pytest.mark.parametrize("subcommand", ["prepare", "activate", "resume", "rollback"])
def test_production_release_mutations_validate_launch_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
    subcommand: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, debug="1")
    arguments = (
        ("--manifest", str(tmp_path / "manifest.json"))
        if subcommand == "prepare"
        else ("--release-id", "release-20260714")
    )

    result = run_wrapper(
        fake_environment,
        "release",
        subcommand,
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_release_activation_contends_with_existing_lifecycle_lock_before_tools(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, environment = fake_environment
    lock_path = Path(f"{environment['SMS_RUNTIME_ROOT']}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(
            fake_environment,
            "release",
            "activate",
            "--release-id",
            "release-20260714",
        )
    finally:
        os.close(descriptor)

    assert result.returncode != 0
    assert "lock" in result.stderr.lower()
    assert command_lines(log) == []


def test_private_locked_release_action_requires_inherited_verified_fd(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "__locked",
        "release",
        "activate",
        "--release-id",
        "release-20260714",
    )

    assert result.returncode == 2
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("--profile", "dev", "up", "-d"),
        ("--env-file", "override.env", "up", "-d"),
        ("--project-name", "other", "up", "-d"),
    ],
)
def test_global_compose_option_as_first_argument_is_rejected_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], arguments: tuple[str, ...]
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, *arguments)

    assert result.returncode == 2
    assert command_lines(log) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "test"),
        ("debug", "1"),
        ("auth_mock", "1"),
        ("vendor_mock", "1"),
    ],
)
def test_production_up_rejects_unsafe_root_env_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], field: str, value: str
) -> None:
    platform_root, log, _ = fake_environment
    values = {"debug": "0", "auth_mock": "0", "vendor_mock": "0"}
    values[field] = value
    write_non_secret_env(platform_root, **values)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SMS_INGRESS_SUBNET", "10.253.42.0/24"),
        ("SMS_API_INGRESS_IPV4", "10.253.42.2"),
        ("SMS_WEB_INGRESS_IPV4", "10.253.42.3"),
    ],
)
def test_production_up_rejects_ingress_override_in_root_env_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], name: str, value: str
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)
    with (platform_root / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ENVIRONMENT", "test"),
        ("DEBUG", "1"),
        ("AUTH_MOCK", "1"),
        ("VENDOR_MOCK", "1"),
        ("COMPOSE_PROFILES", "dev"),
        ("SMS_INGRESS_SUBNET", "10.253.42.0/24"),
        ("SMS_API_INGRESS_IPV4", "10.253.42.2"),
        ("SMS_WEB_INGRESS_IPV4", "10.253.42.3"),
    ],
)
def test_production_up_rejects_unsafe_shell_override_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], name: str, value: str
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production", name: value},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_production_rotate_rejects_env_activated_compose_profile(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, compose_profiles="dev")

    result = run_wrapper(
        fake_environment,
        "rotate",
        "backend",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_production_up_accepts_only_safe_non_secret_settings(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        "--remove-orphans",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0
    prefix = compose_prefix(platform_root)
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", "-d", "--remove-orphans"], runtime=runtime),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("-d", "mock-vendor"),
        ("-d", "unknown-service"),
        ("-d", "--scale", "beat=2"),
        ("-d", "--scale=beat=2"),
        ("-d", "--profile", "dev"),
        ("-d", "--env-file", "override.env"),
        ("-d", "--build"),
        ("-d", "--pull", "always"),
        ("-d", "--wait"),
    ],
)
def test_production_up_rejects_unapproved_options_or_services_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], arguments: tuple[str, ...]
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert command_lines(log) == []


def test_production_up_allows_dba_fixed_service_recreate(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    services = [
        "postgres",
        "api",
                    "worker-realtime",
                    "worker-bulk",
                    "worker-callback",
                    "outbox-dispatcher",
                    "beat",
    ]
    arguments = ["-d", "--no-deps", "--force-recreate", *services]
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0
    prefix = compose_prefix(platform_root)
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", *arguments], runtime=runtime),
    ]


def test_development_up_keeps_mock_profile_path_available(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, _, _ = fake_environment
    write_non_secret_env(
        platform_root,
        debug="1",
        auth_mock="1",
        vendor_mock="1",
        compose_profiles="dev",
    )

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        "--build",
        "mock-vendor",
        extra_environment={"COMPOSE_PROFILES": "dev"},
    )

    assert result.returncode == 0


def test_up_prepares_then_validates_then_starts(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, "up", "-d")

    assert result.returncode == 0
    prefix = compose_prefix(platform_root)
    assert command_lines(log) == [
        expected_revoke_vendor(platform_root, runtime),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", "-d"], runtime=runtime),
    ]


def test_first_up_safely_creates_missing_nested_lock_parent(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    platform_root, log, _ = fake_environment
    runtime = tmp_path / "missing" / "nested" / "secrets"

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_RUNTIME_ROOT": str(runtime)},
    )

    assert result.returncode == 0
    assert runtime.parent.stat().st_mode & 0o777 == 0o700
    assert runtime.parent.stat().st_uid == os.geteuid()
    lock = Path(f"{runtime}.lifecycle.lock")
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600
    assert lock.stat().st_uid == os.geteuid()
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime),
        expected_line([*compose_prefix(platform_root), "config", "--quiet"], runtime=runtime),
        expected_line([*compose_prefix(platform_root), "up", "-d"], runtime=runtime),
    ]


def test_real_python_lock_allows_up_and_down_when_external_flock_is_unusable(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    started = run_wrapper(fake_environment, "up", "-d")
    stopped = run_wrapper(fake_environment, "down", "--remove-orphans")

    assert started.returncode == 0
    assert stopped.returncode == 0
    assert "external-flock-was-called" not in command_lines(log)


def test_relative_wrapper_invocation_still_uses_an_absolute_private_child(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, _, environment = fake_environment

    result = subprocess.run(
        ["./deploy/sms-compose", "up", "-d"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0


def test_lifecycle_lock_is_project_specific_and_survives_runtime_cleanup(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, _, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    expected_lock = Path(f"{runtime}.lifecycle.lock")

    started = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_RUNTIME_ROOT": f"{runtime}/"},
    )

    assert started.returncode == 0
    assert expected_lock.is_file()
    lock_inode = expected_lock.stat().st_ino

    stopped = run_wrapper(fake_environment, "down", "--remove-orphans")

    assert stopped.returncode == 0
    assert expected_lock.is_file()
    assert expected_lock.stat().st_ino == lock_inode


@pytest.mark.parametrize(
    "runtime_root",
    ["relative/runtime", "/", "/safe/../escape"],
)
def test_dangerous_runtime_root_fails_before_preprocessor_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]], runtime_root: str
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_RUNTIME_ROOT": runtime_root},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_symlink_lock_parent_fails_before_preprocessor_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, _ = fake_environment
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_RUNTIME_ROOT": str(linked_parent / "secrets")},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_unsafe_lock_parent_mode_fails_before_preprocessor_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, _ = fake_environment
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_RUNTIME_ROOT": str(unsafe_parent / "secrets")},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_symlink_lock_file_fails_before_preprocessor_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    target = tmp_path / "unrelated-file"
    target.touch(mode=0o600)
    Path(f"{runtime}.lifecycle.lock").symlink_to(target)

    result = run_wrapper(fake_environment, "up", "-d")

    assert result.returncode != 0
    assert command_lines(log) == []


def test_private_locked_action_requires_helper_marker(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, "__locked", "up", "-d")

    assert result.returncode == 2
    assert command_lines(log) == []


def test_forged_legacy_marker_cannot_run_private_cleanup(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "__locked",
        "down",
        extra_environment={"SMS_LIFECYCLE_LOCKED": "1"},
    )

    assert result.returncode == 2
    assert command_lines(log) == []


def test_unlocked_expected_fd_cannot_run_private_cleanup(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    lock_path = Path(f"{runtime}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        result = subprocess.run(
            [str(WRAPPER), "__locked", "down"],
            env={
                **environment,
                "SMS_LIFECYCLE_LOCKED": "1",
                "SMS_LIFECYCLE_LOCK_FD": str(descriptor),
            },
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 2
    assert command_lines(log) == []


def test_unlocked_same_inode_fd_cannot_borrow_another_holders_lock(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    lock_path = Path(f"{runtime}.lifecycle.lock")
    holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(holder, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    unlocked = os.open(lock_path, os.O_RDWR)
    try:
        result = subprocess.run(
            [str(WRAPPER), "__locked", "down"],
            env={
                **environment,
                "SMS_LIFECYCLE_LOCKED": "1",
                "SMS_LIFECYCLE_LOCK_FD": str(unlocked),
            },
            pass_fds=(unlocked,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(unlocked)
        os.close(holder)

    assert result.returncode == 2
    assert command_lines(log) == []


def test_locked_wrong_inode_fd_cannot_run_private_cleanup(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, environment = fake_environment
    unrelated = tmp_path / "unrelated.lock"
    descriptor = os.open(unrelated, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            [str(WRAPPER), "__locked", "down"],
            env={
                **environment,
                "SMS_LIFECYCLE_LOCKED": "1",
                "SMS_LIFECYCLE_LOCK_FD": str(descriptor),
            },
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 2
    assert command_lines(log) == []


def test_prepare_failure_prevents_compose(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, "up", "-d", extra_environment={"FAKE_PYTHON_EXIT": "17"})

    assert result.returncode == 17
    assert command_lines(log) == [expected_prepare(platform_root, runtime)]


def test_config_failure_prevents_up(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, "up", "-d", extra_environment={"FAKE_CONFIG_EXIT": "19"})

    assert result.returncode == 19
    prefix = compose_prefix(platform_root)
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
    ]


def test_down_cleans_only_after_compose_succeeds(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, "down", "--remove-orphans", "-v")

    assert result.returncode == 0
    assert command_lines(log) == [
        expected_line(
            [*compose_prefix(platform_root), "down", "--remove-orphans", "-v"],
            runtime=runtime,
        ),
        expected_line([*compose_prefix(platform_root), "ps", "--all", "-q"], runtime=runtime),
        expected_line(
            [
                "python3",
                str(platform_root / "deploy" / "scripts" / "prepare_runtime_secrets.py"),
                "cleanup",
                "--runtime-root",
                str(runtime),
                "--all",
            ]
        ),
    ]


@pytest.mark.parametrize("argument", ["--help", "--dry-run", "--timeout", "unknown"])
def test_down_rejects_unapproved_argument_without_compose_or_cleanup(
    fake_environment: tuple[Path, Path, dict[str, str]], argument: str
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, "down", argument)

    assert result.returncode == 2
    assert command_lines(log) == []


def test_down_does_not_clean_when_stopped_or_exited_project_containers_remain(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(
        fake_environment,
        "down",
        "--volumes",
        extra_environment={"FAKE_PS_OUTPUT": "container-id\n"},
    )

    assert result.returncode != 0
    assert command_lines(log) == [
        expected_line([*compose_prefix(platform_root), "down", "--volumes"], runtime=runtime),
        expected_line([*compose_prefix(platform_root), "ps", "--all", "-q"], runtime=runtime),
    ]


def test_failed_down_does_not_clean_runtime_secrets(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, "down", extra_environment={"FAKE_DOWN_EXIT": "23"})

    assert result.returncode == 23
    assert command_lines(log) == [
        expected_line([*compose_prefix(platform_root), "down"], runtime=runtime)
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("config", "--quiet"),
        ("ps",),
        ("logs", "api"),
        ("exec", "api", "id"),
        ("cp", "api:/tmp/dev-apikeys.txt", "deploy/secrets/dev-apikeys.txt"),
    ],
)
def test_allowlisted_diagnostic_commands_keep_fixed_compose_inputs(
    fake_environment: tuple[Path, Path, dict[str, str]], arguments: tuple[str, ...]
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])

    result = run_wrapper(fake_environment, *arguments)

    assert result.returncode == 0
    assert command_lines(log) == [
        expected_line([*compose_prefix(platform_root), *arguments], runtime=runtime)
    ]


def test_production_rejects_cp_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "cp",
        "api:/tmp/dev-apikeys.txt",
        "deploy/secrets/dev-apikeys.txt",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("start",),
        ("create",),
        ("restart",),
        ("stop",),
        ("kill",),
        ("rm",),
        ("pause",),
        ("unpause",),
        ("pull",),
        ("build",),
        ("run", "mock-vendor"),
        ("run", "--rm", "mock-vendor"),
        ("run", "migrate"),
        ("run", "--rm", "migrate", "extra"),
        ("partition-maintenance", "--apply"),
        ("partition-maintenance", "--dry-run", "extra"),
        ("unknown",),
    ],
)
def test_mutating_or_unknown_actions_are_rejected_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], arguments: tuple[str, ...]
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, *arguments)

    assert result.returncode == 2
    assert command_lines(log) == []


def test_production_run_rm_migrate_validates_prepares_and_uses_fixed_command(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    prefix = compose_prefix(platform_root)
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "run",
        "--rm",
        "migrate",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "run", "--rm", "migrate"], runtime=runtime),
    ]


def test_production_run_rm_migrate_rejects_unsafe_env_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, auth_mock="1")

    result = run_wrapper(
        fake_environment,
        "run",
        "--rm",
        "migrate",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_partition_maintenance_is_locked_and_uses_fixed_owner_command(
    fake_environment: tuple[Path, Path, dict[str, str]],
    dry_run: bool,
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    prefix = compose_prefix(platform_root)
    write_non_secret_env(platform_root)
    arguments = ["partition-maintenance"]
    if dry_run:
        arguments.append("--dry-run")

    result = run_wrapper(
        fake_environment,
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    owner_command = [
        *prefix,
        "run",
        "--rm",
        "migrate",
        "python",
        "-m",
        "scripts_support.maintain_partitions",
        "--max-attempts",
        "3",
    ]
    if dry_run:
        owner_command.append("--dry-run")
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line(owner_command, runtime=runtime),
    ]


def test_production_run_rm_mock_vendor_is_rejected_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "run",
        "--rm",
        "mock-vendor",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert command_lines(log) == []


def test_wrapper_output_never_contains_fixture_secret_values(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, _, _ = fake_environment
    fixture_value = "fixture-secret-value-never-log"
    secret = platform_root / "deploy" / "secrets" / "jwt_secret"
    secret.write_text(fixture_value, encoding="utf-8")
    secret.chmod(0o600)

    result = run_wrapper(fake_environment, "up", "-d")

    assert result.returncode == 0
    assert fixture_value not in result.stdout
    assert fixture_value not in result.stderr


def test_rotate_backend_recreates_only_runtime_backend_services(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    prefix = compose_prefix(platform_root)

    result = run_wrapper(fake_environment, "rotate", "backend")

    assert result.returncode == 0
    old_target = "generations/generation-00000000000000000000000000000000"
    assert command_lines(log) == [
        expected_preprocessor(platform_root, runtime, "current-target"),
        expected_prepare(platform_root, runtime),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line(
            [
                *prefix,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "redis",
                "redis-auth",
                "redis-control",
                "api",
                "worker-realtime",
                "worker-bulk",
                "worker-callback",
                "outbox-dispatcher",
                "beat",
            ],
            runtime=runtime,
        ),
    ]
    assert not any("|cleanup|" in line for line in command_lines(log))
    assert old_target not in result.stdout


def test_concurrent_rotate_fails_before_second_process_calls_python_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, environment = fake_environment
    entered = tmp_path / "first-rotate-entered"
    release = tmp_path / "release-first-rotate"
    first_environment = environment.copy()
    first_environment.update(
        {
            "FAKE_UP_ENTERED_FILE": str(entered),
            "FAKE_UP_RELEASE_FILE": str(release),
        }
    )
    first = subprocess.Popen(
        [str(WRAPPER), "rotate", "backend"],
        env=first_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "first rotate did not reach the Compose recreate"
        before_second = command_lines(log)

        second = run_wrapper(
            fake_environment,
            "rotate",
            "backend",
            extra_environment={"SMS_RUNTIME_ROOT": f"{environment['SMS_RUNTIME_ROOT']}/"},
        )

        assert second.returncode != 0
        assert "lock" in second.stderr.lower()
        assert command_lines(log) == before_second
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=5)
    assert first.returncode == 0, (stdout, stderr)


@pytest.mark.parametrize(
    ("helper_signal", "expected_returncode"),
    [(signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGKILL, -signal.SIGKILL)],
)
def test_child_inherited_lock_survives_helper_signal_until_child_exits(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
    helper_signal: signal.Signals,
    expected_returncode: int,
) -> None:
    _, log, _ = fake_environment
    entered = tmp_path / f"helper-{helper_signal.name.lower()}-entered"
    release = tmp_path / f"release-{helper_signal.name.lower()}"
    helper = start_lock_helper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "FAKE_UP_ENTERED_FILE": str(entered),
            "FAKE_UP_RELEASE_FILE": str(release),
        },
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "locked child did not reach Compose startup"
        before_second = command_lines(log)

        os.kill(helper.pid, helper_signal)
        if helper_signal == signal.SIGKILL:
            helper.wait(timeout=5)
        else:
            time.sleep(0.1)

        blocked_down = run_wrapper(fake_environment, "down", "--remove-orphans")

        assert blocked_down.returncode != 0
        assert "lock" in blocked_down.stderr.lower()
        assert command_lines(log) == before_second
    finally:
        release.touch()
        stdout, stderr = helper.communicate(timeout=5)
    assert helper.returncode == expected_returncode, (stdout, stderr)

    later_down = blocked_down
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        later_down = run_wrapper(fake_environment, "down", "--remove-orphans")
        if later_down.returncode == 0:
            break
        time.sleep(0.02)
    assert later_down.returncode == 0


def test_release_helper_term_reaches_manager_and_releases_lock(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    entered = tmp_path / "release-manager-entered"
    release = tmp_path / "release-manager-release"
    helper = start_lock_helper(
        fake_environment,
        "release",
        "activate",
        "--release-id",
        "release-20260715",
        extra_environment={
            "FAKE_RELEASE_ENTERED_FILE": str(entered),
            "FAKE_RELEASE_RELEASE_FILE": str(release),
        },
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "release manager did not start"

        os.kill(helper.pid, signal.SIGTERM)
        stdout, stderr = helper.communicate(timeout=5)
        assert helper.returncode == 128 + signal.SIGTERM, (stdout, stderr)

        later_down = run_wrapper(fake_environment, "down", "--remove-orphans")
        assert later_down.returncode == 0, later_down.stderr
    finally:
        release.touch()


def test_rotate_lock_rejects_down_without_cleanup_then_releases(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, environment = fake_environment
    entered = tmp_path / "rotate-entered"
    release = tmp_path / "release-rotate"
    first_environment = environment.copy()
    first_environment.update(
        {
            "FAKE_UP_ENTERED_FILE": str(entered),
            "FAKE_UP_RELEASE_FILE": str(release),
        }
    )
    first = subprocess.Popen(
        [str(WRAPPER), "rotate", "backend"],
        env=first_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "rotate did not reach the Compose recreate"
        before_down = command_lines(log)

        blocked_down = run_wrapper(fake_environment, "down", "--remove-orphans")

        assert blocked_down.returncode != 0
        assert "lock" in blocked_down.stderr.lower()
        assert command_lines(log) == before_down
        assert not any("|cleanup|" in line for line in command_lines(log))
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=5)
    assert first.returncode == 0, (stdout, stderr)

    later_down = run_wrapper(fake_environment, "down", "--remove-orphans")

    assert later_down.returncode == 0
    assert any("|cleanup|" in line for line in command_lines(log))


def test_up_lock_rejects_rotate_before_any_new_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    _, log, environment = fake_environment
    entered = tmp_path / "up-entered"
    release = tmp_path / "release-up"
    first_environment = environment.copy()
    first_environment.update(
        {
            "FAKE_UP_ENTERED_FILE": str(entered),
            "FAKE_UP_RELEASE_FILE": str(release),
        }
    )
    first = subprocess.Popen(
        [str(WRAPPER), "up", "-d"],
        env=first_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "up did not reach Compose startup"
        before_rotate = command_lines(log)

        blocked_rotate = run_wrapper(fake_environment, "rotate", "backend")

        assert blocked_rotate.returncode != 0
        assert "lock" in blocked_rotate.stderr.lower()
        assert command_lines(log) == before_rotate
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=5)
    assert first.returncode == 0, (stdout, stderr)


def test_rotate_backend_failure_reactivates_old_generation_and_recovers_services(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    prefix = compose_prefix(platform_root)
    old_target = "generations/generation-00000000000000000000000000000000"
    recreate = [
        *prefix,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "120",
        "redis",
        "redis-auth",
        "redis-control",
        "api",
        "worker-realtime",
        "worker-bulk",
        "worker-callback",
        "outbox-dispatcher",
        "beat",
    ]

    result = run_wrapper(
        fake_environment,
        "rotate",
        "backend",
        extra_environment={"FAKE_UP_FAIL_THROUGH": "1", "FAKE_UP_EXIT": "29"},
    )

    assert result.returncode == 29
    assert command_lines(log) == [
        expected_preprocessor(platform_root, runtime, "current-target"),
        expected_prepare(platform_root, runtime),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line(recreate, runtime=runtime),
        expected_preprocessor(platform_root, runtime, "activate", "--target", old_target),
        expected_line(recreate, runtime=runtime),
    ]


def test_rotate_backend_reports_failed_recovery_and_never_cleans_old_generation(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    old_target = "generations/generation-00000000000000000000000000000000"

    result = run_wrapper(
        fake_environment,
        "rotate",
        "backend",
        extra_environment={"FAKE_UP_FAIL_THROUGH": "2", "FAKE_UP_EXIT": "31"},
    )

    assert result.returncode != 0
    lines = command_lines(log)
    assert (
        expected_preprocessor(platform_root, runtime, "activate", "--target", old_target) in lines
    )
    assert not any("|cleanup|" in line for line in lines)
    assert "recovery failed" in result.stderr.lower()


@pytest.mark.parametrize("group", ["database", "postgres", "all"])
def test_invalid_rotation_group_exits_two_without_compose_mutation(
    fake_environment: tuple[Path, Path, dict[str, str]], group: str
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, "rotate", group)

    assert result.returncode == 2
    assert command_lines(log) == []
