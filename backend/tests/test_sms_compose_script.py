from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy" / "sms-compose"
PRODUCTION_LAUNCHER = ROOT / "deploy" / "production-sms-compose-launcher"
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
    (platform_root / "deploy" / "docker-compose.production-storage.yml").write_text(
        "volumes: {}\n", encoding="utf-8"
    )
    (platform_root / "deploy" / "docker-compose.production-restart.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    (platform_root / "deploy" / "docker-compose.redis-tls.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
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
    renderer = platform_root / "deploy" / "scripts" / "render_trusted_proxy_conf.py"
    renderer.write_text(
        (ROOT / "deploy/scripts/render_trusted_proxy_conf.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    for script_name, exit_variable in (
        ("storage_preflight.py", "FAKE_STORAGE_PREFLIGHT_EXIT"),
        ("redis_tls_preflight.py", "FAKE_REDIS_TLS_PREFLIGHT_EXIT"),
        ("volume_contract_preflight.py", "FAKE_VOLUME_PREFLIGHT_EXIT"),
    ):
        (platform_root / "deploy" / "scripts" / script_name).write_text(
            "from __future__ import annotations\n"
            "import os\n"
            f"raise SystemExit(int(os.environ.get('{exit_variable}', '0')))\n",
            encoding="utf-8",
        )
    redis_tls_rotation_guard = (
        platform_root / "deploy" / "scripts" / "redis_tls_rotation_guard.py"
    )
    redis_tls_rotation_guard.write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "arguments = ['python3', __file__, *sys.argv[1:]]\n"
        "with Path(os.environ['COMMAND_LOG']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write('|'.join(arguments) + '\\n')\n"
        "raise SystemExit(int(os.environ.get('FAKE_REDIS_TLS_ROTATION_GUARD_EXIT', '0')))\n",
        encoding="utf-8",
    )
    transport_verifier = platform_root / "scripts" / "verify_web_transport.py"
    transport_verifier.parent.mkdir()
    transport_verifier.write_text(
        (ROOT / "scripts" / "verify_web_transport.py").read_text(encoding="utf-8"),
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
    continuity_manager = (
        platform_root / "deploy" / "scripts" / "continuity_manager.py"
    )
    continuity_manager.write_text(
        """from __future__ import annotations

import os
import sys
from pathlib import Path

arguments = ["python3", __file__, *sys.argv[1:]]
exit_status = int(os.environ.get("FAKE_CONTINUITY_EXIT", "0"))
if exit_status != 0 or os.environ.get("FAKE_CONTINUITY_LOG") == "1":
    with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write("|".join(arguments) + "\\n")
raise SystemExit(exit_status)
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
    for script_name in (
        "host_python_preflight.py",
        "lifecycle_manager.py",
        "collect_security_daily_evidence.py",
        "production_control_snapshot.py",
    ):
        (platform_root / "deploy" / "scripts" / script_name).write_text(
            "from __future__ import annotations\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['COMMAND_LOG']).open('a', encoding='utf-8') as stream:\n"
            "    stream.write('|'.join(['python3', __file__, *sys.argv[1:]]) + '\\n')\n",
            encoding="utf-8",
        )
    (platform_root / ".env").write_text(
        "# non-secret fixture\n"
        "SMS_EXTERNAL_TLS_MODE=0\n"
        "SMS_TRUSTED_PROXY_CIDRS=\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    write_executable(
        fake_bin / "stat",
        """#!/bin/sh
printf '%s\n' "${FAKE_STAT_OUTPUT:-0:0:600:regular file}"
""",
    )
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
            "SMS_PRODUCTION_CONTROL_SMOKE": "1",
            "SMS_TRUSTED_PROXY_CONF": str(
                tmp_path / "trusted-proxy-output" / "trusted-proxies.conf"
            ),
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
        "WEB_BIND_IP",
        "SMS_EXTERNAL_TLS_MODE",
        "SMS_TRUSTED_PROXY_CIDRS",
        "REDIS_HA_MODE",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "SMS_API_IMAGE",
        "SMS_WEB_IMAGE",
        "SMS_POSTGRES_IMAGE",
        "SMS_REDIS_IMAGE",
        "METRICS_ALLOWED_CIDRS",
        "SMS_INTERNAL_PLATFORM_ENV_FILE",
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


def create_smoke_control_snapshot(platform_root: Path, parent: Path) -> Path:
    control_root = parent / "production-control" / "versions" / ("a" * 40)
    shutil.copytree(platform_root, control_root)
    write_executable(
        control_root / "deploy" / "sms-compose",
        WRAPPER.read_text(encoding="utf-8"),
    )
    return control_root


def compose_prefix(
    platform_root: Path,
    *,
    production: bool = False,
    redis_ha_mode: str = "isolated-standalone",
) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(platform_root / ".env"),
        "-f",
        str(platform_root / "deploy" / "docker-compose.yml"),
    ]
    if production:
        command.extend(
            [
                "-f",
                str(platform_root / "deploy" / "docker-compose.production-storage.yml"),
                "-f",
                str(platform_root / "deploy" / "docker-compose.production-restart.yml"),
            ]
        )
        if redis_ha_mode == "isolated-standalone":
            command.extend(
                [
                    "-f",
                    str(platform_root / "deploy" / "docker-compose.redis-tls.yml"),
                ]
            )
    return command


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


def expected_redis_tls_rotation_guard(
    platform_root: Path,
    runtime: Path,
    baseline_target: str,
) -> str:
    return expected_line(
        [
            "python3",
            str(platform_root / "deploy" / "scripts" / "redis_tls_rotation_guard.py"),
            "--source-dir",
            str(platform_root / "deploy" / "secrets"),
            "--runtime-root",
            str(runtime),
            "--baseline-target",
            baseline_target,
        ]
    )


def seed_runtime_generation(
    runtime: Path,
    target: str = "generations/generation-00000000000000000000000000000000",
) -> str:
    (runtime / target).mkdir(parents=True)
    (runtime / "current").symlink_to(target)
    return target


def expected_release_manager(
    platform_root: Path,
    mode: str,
    *arguments: str,
    release_root: str = "/var/lib/sms-platform/releases",
    control_root: Path | None = None,
) -> str:
    effective_control_root = control_root or platform_root
    roots = (
        [
            "--platform-root",
            str(platform_root),
            "--control-root",
            str(effective_control_root),
        ]
        if mode == "production"
        else ["--root", str(platform_root)]
    )
    return expected_line(
        [
            "python3",
            str(effective_control_root / "deploy" / "scripts" / "release_manager.py"),
            *roots,
            "--environment-file",
            str(platform_root / ".env"),
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
    web_bind_ip: str | None = None,
    external_tls_mode: str | None = None,
    trusted_proxy_cidrs: str | None = None,
    redis_ha_mode: str | None = "isolated-standalone",
    metrics_allowed_cidrs: str | None = "172.31.250.1/32",
) -> None:
    lines = [
        f"ENVIRONMENT={environment}",
        f"DEBUG={debug}",
        f"AUTH_MOCK={auth_mock}",
        f"VENDOR_MOCK={vendor_mock}",
    ]
    if redis_ha_mode is not None:
        lines.append(f"REDIS_HA_MODE={redis_ha_mode}")
    if metrics_allowed_cidrs is not None:
        lines.append(f"METRICS_ALLOWED_CIDRS={metrics_allowed_cidrs}")
    if compose_profiles is not None:
        lines.append(f"COMPOSE_PROFILES={compose_profiles}")
    if web_bind_ip is not None:
        lines.append(f"WEB_BIND_IP={web_bind_ip}")
    if external_tls_mode is not None:
        lines.append(f"SMS_EXTERNAL_TLS_MODE={external_tls_mode}")
    if trusted_proxy_cidrs is not None:
        lines.append(f"SMS_TRUSTED_PROXY_CIDRS={trusted_proxy_cidrs}")
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


def test_production_source_wrapper_is_rejected_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, environment = fake_environment
    environment = environment.copy()
    environment.update(
        {
            "SMS_SECRETS_MODE": "production",
            "SMS_PRODUCTION_CONTROL_SMOKE": "0",
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "ps"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "immutable production-control snapshot entry" in result.stderr
    assert command_lines(log) == []


def test_snapshot_wrapper_separates_project_data_from_control_bytes(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root)
    control_root = create_smoke_control_snapshot(platform_root, tmp_path)
    snapshot_wrapper = control_root / "deploy" / "sms-compose"
    environment = environment.copy()
    environment["SMS_SECRETS_MODE"] = "production"

    result = subprocess.run(
        [str(snapshot_wrapper), "config"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_line(
            [
                "docker",
                "compose",
                "--env-file",
                str(platform_root / ".env"),
                "-f",
                str(control_root / "deploy" / "docker-compose.yml"),
                "-f",
                str(control_root / "deploy" / "docker-compose.production-storage.yml"),
                "-f",
                str(control_root / "deploy" / "docker-compose.production-restart.yml"),
                "-f",
                str(control_root / "deploy" / "docker-compose.redis-tls.yml"),
                "config",
            ],
            runtime=Path(environment["SMS_RUNTIME_ROOT"]),
        )
    ]


def test_wrapper_rejects_internal_compose_env_shell_and_dotenv_overrides(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment

    shell_override = run_wrapper(
        fake_environment,
        "ps",
        extra_environment={"SMS_INTERNAL_PLATFORM_ENV_FILE": "/tmp/evil.env"},
    )
    assert shell_override.returncode == 1
    assert "shell override" in shell_override.stderr

    (platform_root / ".env").write_text(
        "SMS_INTERNAL_PLATFORM_ENV_FILE=/tmp/evil.env\n",
        encoding="utf-8",
    )
    dotenv_override = run_wrapper(fake_environment, "ps")
    assert dotenv_override.returncode == 1
    assert "root .env override" in dotenv_override.stderr
    assert command_lines(log) == []


def test_production_launcher_is_fixed_and_rejects_control_overrides() -> None:
    assert PRODUCTION_LAUNCHER.is_file()
    assert os.access(PRODUCTION_LAUNCHER, os.X_OK)
    source = PRODUCTION_LAUNCHER.read_text(encoding="utf-8")
    for token in (
        "/usr/local/libexec/sms-platform/production-control",
        "/etc/sms-platform/production-control-approved",
        "_acquire_lifecycle_lock",
        "_resolve_verified_wrapper",
        "_validate_approved_commit",
        "_validate_snapshot",
        ".sms-platform-production-control-manifest.json",
        "hasher = hashlib.sha256()",
        'getattr(os, "O_NOFOLLOW", 0)',
        "os.execve(wrapper",
        '"PYTHONDONTWRITEBYTECODE": "1"',
        'environment["SMS_LIFECYCLE_LOCKED"] = "1"',
    ):
        assert token in source
    main_source = source[source.index("def main()") :]
    assert main_source.index("descriptor = _acquire_lifecycle_lock()") < main_source.index(
        "wrapper = _resolve_verified_wrapper()"
    )

    environment = {
        "PATH": os.environ["PATH"],
        "SMS_PRODUCTION_CONTROL_ROOT": "/tmp/attacker-control",
    }
    result = subprocess.run(
        [str(PRODUCTION_LAUNCHER), "status"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "production environment override is forbidden" in result.stderr

    for name, value in (
        ("SMS_API_IMAGE", "registry.invalid/api@sha256:" + "0" * 64),
        ("COMPOSE_FILE", "/tmp/attacker-compose.yml"),
        ("DOCKER_HOST", "tcp://attacker.invalid:2375"),
    ):
        injected = {"PATH": os.environ["PATH"], name: value}
        result = subprocess.run(
            [str(PRODUCTION_LAUNCHER), "status"],
            env=injected,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 1
        assert f"production environment override is forbidden: {name}" in result.stderr


def test_snapshot_root_jobs_use_control_python_and_share_lifecycle_lock(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root)
    control_root = create_smoke_control_snapshot(platform_root, tmp_path)
    wrapper = control_root / "deploy" / "sms-compose"
    environment = environment.copy()
    environment["SMS_SECRETS_MODE"] = "production"

    lifecycle = subprocess.run(
        [str(wrapper), "host-lifecycle", "backup"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    security = subprocess.run(
        [str(wrapper), "security-report"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert lifecycle.returncode == 0, lifecycle.stderr
    assert security.returncode == 0, security.stderr
    assert command_lines(log) == [
        expected_line(
            [
                "python3",
                str(control_root / "deploy" / "scripts" / "host_python_preflight.py"),
                "lifecycle",
            ]
        ),
        expected_line(
            [
                "python3",
                str(control_root / "deploy" / "scripts" / "lifecycle_manager.py"),
                "--root",
                str(platform_root),
                "--control-root",
                str(control_root),
                "backup",
            ]
        ),
        expected_line(
            [
                "python3",
                str(
                    control_root
                    / "deploy"
                    / "scripts"
                    / "collect_security_daily_evidence.py"
                ),
                "--auth-log",
                "/var/log/auth.log",
                "--web-log",
                str(platform_root / "deploy" / "security-report-nginx" / "access.log"),
                "--output-dir",
                str(platform_root / "deploy" / "security-report-control" / "incoming"),
            ]
        ),
    ]


def test_snapshot_host_control_forwards_exact_commit_and_locks_mutations(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root)
    control_root = create_smoke_control_snapshot(platform_root, tmp_path)
    wrapper = control_root / "deploy" / "sms-compose"
    expected_commit = "b" * 40
    environment = environment.copy()
    environment["SMS_SECRETS_MODE"] = "production"

    plan = subprocess.run(
        [
            str(wrapper),
            "host-control",
            "plan",
            "--expected-commit",
            expected_commit,
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    prepare = subprocess.run(
        [
            str(wrapper),
            "host-control",
            "prepare",
            "--expected-commit",
            expected_commit,
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        [str(wrapper), "host-control", "status"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert plan.returncode == 0, plan.stderr
    assert prepare.returncode == 0, prepare.stderr
    assert status.returncode == 0, status.stderr
    manager = str(control_root / "deploy" / "scripts" / "production_control_snapshot.py")
    assert command_lines(log) == [
        expected_line(
            [
                "python3",
                manager,
                "plan",
                "--expected-commit",
                expected_commit,
            ]
        ),
        expected_line(
            [
                "python3",
                manager,
                "prepare",
                "--expected-commit",
                expected_commit,
            ]
        ),
        expected_line(["python3", manager, "status"]),
    ]


def test_snapshot_wrapper_reuses_launcher_inherited_lifecycle_lock(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root)
    control_root = create_smoke_control_snapshot(platform_root, tmp_path)
    wrapper = control_root / "deploy" / "sms-compose"
    lock_path = Path(f"{environment['SMS_RUNTIME_ROOT']}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    environment = environment.copy()
    environment.update(
        {
            "SMS_SECRETS_MODE": "production",
            "SMS_LIFECYCLE_LOCKED": "1",
            "SMS_LIFECYCLE_LOCK_FD": str(descriptor),
        }
    )
    try:
        result = subprocess.run(
            [str(wrapper), "host-lifecycle", "backup-status"],
            env=environment,
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.close(descriptor)

    assert result.returncode == 0, result.stderr
    assert any("host_python_preflight.py|lifecycle" in line for line in command_lines(log))
    assert any("lifecycle_manager.py|--root" in line for line in command_lines(log))


@pytest.mark.parametrize(
    "name",
    (
        "SMS_SECURITY_REPORT_CONTROL_DIR",
        "SMS_SECURITY_REPORT_CONFIG_DIR",
        "SMS_SECURITY_REPORT_NGINX_DIR",
    ),
)
def test_production_rejects_security_report_path_overrides(
    fake_environment: tuple[Path, Path, dict[str, str]],
    name: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)
    shell = run_wrapper(
        fake_environment,
        "ps",
        extra_environment={"SMS_SECRETS_MODE": "production", name: "/tmp/escape"},
    )
    assert shell.returncode == 1
    assert "security-report path overrides" in shell.stderr
    assert command_lines(log) == []

    with (platform_root / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"{name}=/tmp/escape\n")
    dotenv = run_wrapper(
        fake_environment,
        "ps",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )
    assert dotenv.returncode == 1
    assert "Compose topology overrides" in dotenv.stderr
    assert command_lines(log) == []


def test_production_host_state_paths_are_fixed_and_prevalidated() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    for token in (
        'SOURCE_SECRETS_ROOT="/etc/sms-platform/secrets"',
        'PLATFORM_ENV_FILE="/etc/sms-platform/platform.env"',
        '--environment-file "$PLATFORM_ENV_FILE"',
        '--secrets-dir "$SOURCE_SECRETS_ROOT"',
        '--source-dir "$SOURCE_SECRETS_ROOT"',
        'PRODUCTION_SECURITY_REPORT_ROOT="/var/lib/sms-platform/security-report"',
        '"$PRODUCTION_SECURITY_REPORT_CONTROL_DIR" "0:0:755:directory"',
        '"$PRODUCTION_SECURITY_REPORT_INCOMING_DIR" "0:10001:750:directory"',
        '"$PRODUCTION_SECURITY_REPORT_REQUESTS_DIR" "10001:10001:700:directory"',
        '"$PRODUCTION_SECURITY_REPORT_RESULTS_DIR" "10001:10001:700:directory"',
        '"$PRODUCTION_SECURITY_REPORT_CONFIG_DIR" "10001:10001:700:directory"',
        '"$PRODUCTION_SECURITY_REPORT_NGINX_DIR" "101:101:750:directory"',
        "--fixed-production-output",
    ):
        assert token in source
    security_path_function = source[
        source.index("configure_security_report_paths()") : source.index(
            "configure_compose_topology()"
        )
    ]
    assert "mkdir" not in security_path_function


def test_usage_lists_only_explicitly_allowed_actions() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert "<compose-command>" not in source
    assert "config|ps|logs" in source
    assert "generic exec is forbidden in production mode" in source
    assert "run --rm migrate" in source
    assert "partition-maintenance [--dry-run]" in source
    assert "production up options" in source
    assert "production up services" in source
    assert "command -v flock" not in source
    assert "run_with_lifecycle_lock.py" in source
    assert "release prepare|bootstrap|start-recovery" in source
    assert "prepare-forward-rollback|activate|status|resume|rollback" in source
    assert "activate|status|resume|rollback" in source
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


def test_production_continuity_gate_blocks_up_before_secrets_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_CONTINUITY_EXIT": "1",
        },
    )

    assert result.returncode == 1
    lines = command_lines(log)
    assert len(lines) == 1
    assert "continuity_manager.py|--root" in lines[0]
    assert f"|--control-root|{platform_root}|" in lines[0]
    assert f"|--environment-file|{platform_root / '.env'}|" in lines[0]
    assert lines[0].endswith("|--mode|production|gate")
    assert not any(line.startswith("docker|") for line in lines)
    assert not any("prepare_runtime_secrets.py" in line for line in lines)


def test_production_continuity_bad_evidence_and_init_admin_gate_call_zero_docker(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)
    digest = "a" * 64

    engage = run_wrapper(
        fake_environment,
        "continuity",
        "engage",
        "--evidence",
        "/tmp/continuity-engage.json",
        "--evidence-sha256",
        digest,
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_CONTINUITY_EXIT": "1",
        },
    )
    init_admin = run_wrapper_with_tty(
        fake_environment,
        "init-admin",
        "--show-temporary-password",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_CONTINUITY_EXIT": "1",
        },
    )

    assert engage.returncode == 1
    assert init_admin == 1
    lines = command_lines(log)
    assert any("|engage|--evidence|/tmp/continuity-engage.json|" in line for line in lines)
    assert not any(line.startswith("docker|") for line in lines)
    assert not any("prepare_runtime_secrets.py" in line for line in lines)


def test_continuity_control_is_production_only(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(fake_environment, "continuity", "status")

    assert result.returncode == 2
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


def test_production_release_prepare_is_locked_without_preparing_runtime_secrets(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, _ = fake_environment
    manifest = tmp_path / "staging" / "manifest.json"
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "release",
        "prepare",
        "--manifest",
        str(manifest),
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_release_manager(
            platform_root,
            "production",
            "prepare",
            "--manifest",
            str(manifest),
        )
    ]


def test_production_release_prepare_fails_closed_when_lifecycle_lock_is_held(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    manifest = tmp_path / "staging" / "manifest.json"
    write_non_secret_env(platform_root)
    lock_path = Path(f"{environment['SMS_RUNTIME_ROOT']}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(
            fake_environment,
            "release",
            "prepare",
            "--manifest",
            str(manifest),
            extra_environment={"SMS_SECRETS_MODE": "production"},
        )
    finally:
        os.close(descriptor)

    assert result.returncode != 0
    assert "lifecycle lock is already held" in result.stderr
    assert command_lines(log) == []


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


def test_production_bootstrap_is_locked_prepares_secrets_and_preserves_confirmation(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    manifest = tmp_path / "staging" / "manifest.json"
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "release",
        "bootstrap",
        "--manifest",
        str(manifest),
        "--confirm-empty-host",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_prepare(platform_root, runtime, mode="production"),
        expected_release_manager(
            platform_root,
            "production",
            "bootstrap",
            "--manifest",
            str(manifest),
            "--confirm-empty-host",
        ),
    ]


@pytest.mark.parametrize(
    ("subcommand", "argument_kind", "prepares_secrets"),
    (
        ("start-recovery", "start", True),
        ("observe-recovery", "observe", False),
        ("adopt-recovery", "adopt", False),
        ("resume-recovery", "resume", False),
    ),
)
def test_production_recovery_actions_are_locked_and_forward_exact_bindings(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
    subcommand: str,
    argument_kind: str,
    prepares_secrets: bool,
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    manifest = tmp_path / "staging" / "manifest.json"
    snapshot = tmp_path / "snapshot" / "manifest.json"
    receipt = tmp_path / "evidence" / "restore-receipt.json"
    gap_fence = tmp_path / "evidence" / "gap-fence.json"
    output = tmp_path / "evidence" / "pending-receipt.json"
    digest = "a" * 64
    arguments: tuple[str, ...]
    if argument_kind == "start":
        arguments = (
            "--manifest",
            str(manifest),
            "--snapshot-manifest",
            str(snapshot),
            "--snapshot-manifest-sha256",
            digest,
            "--confirm-recovered-host",
        )
    elif argument_kind == "observe":
        arguments = (
            "--manifest",
            str(manifest),
            "--snapshot-manifest",
            str(snapshot),
            "--snapshot-manifest-sha256",
            digest,
            "--output",
            str(output),
            "--confirm-recovered-host",
        )
    elif argument_kind == "adopt":
        arguments = (
            "--manifest",
            str(manifest),
            "--snapshot-manifest",
            str(snapshot),
            "--snapshot-manifest-sha256",
            digest,
            "--restore-receipt",
            str(receipt),
            "--restore-receipt-sha256",
            digest,
            "--gap-fence-evidence",
            str(gap_fence),
            "--gap-fence-sha256",
            digest,
            "--confirm-recovered-host",
        )
    else:
        arguments = ("--stage", "workers", "--confirm-recovered-host")
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "release",
        subcommand,
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    runtime_target = "generations/generation-00000000000000000000000000000000"
    expected = []
    if prepares_secrets:
        expected.append(expected_prepare(platform_root, runtime, mode="production"))
    expected.extend(
        [
            expected_preprocessor(platform_root, runtime, "current-target"),
            expected_release_manager(
                platform_root,
                "production",
                subcommand,
                *arguments,
                "--runtime-secrets-target",
                runtime_target,
            ),
        ]
    )
    assert command_lines(log) == expected


def test_production_forward_rollback_candidate_is_locked_without_reseeding_secrets(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    platform_root, log, _ = fake_environment
    manifest = tmp_path / "staging" / "manifest.json"
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "release",
        "prepare-forward-rollback",
        "--source-release-id",
        "release-20260714",
        "--manifest",
        str(manifest),
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_release_manager(
            platform_root,
            "production",
            "prepare-forward-rollback",
            "--source-release-id",
            "release-20260714",
            "--manifest",
            str(manifest),
        )
    ]


@pytest.mark.parametrize("subcommand", ("bootstrap", "prepare-forward-rollback"))
def test_new_production_release_actions_are_unavailable_in_development(
    fake_environment: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
    subcommand: str,
) -> None:
    _, log, _ = fake_environment
    manifest = tmp_path / "staging" / "manifest.json"
    arguments = (
        ("--manifest", str(manifest), "--confirm-empty-host")
        if subcommand == "bootstrap"
        else (
            "--source-release-id",
            "release-20260714",
            "--manifest",
            str(manifest),
        )
    )

    result = run_wrapper(fake_environment, "release", subcommand, *arguments)

    assert result.returncode == 2
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
    "metadata",
    (
        "0:0:644:regular file",
        "501:20:600:regular file",
        "0:0:600:symbolic link",
    ),
)
def test_production_rejects_unsafe_root_env_metadata_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    metadata: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_STAT_OUTPUT": metadata,
        },
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
    prefix = compose_prefix(platform_root, production=True)
    assert command_lines(log) == [
        expected_release_manager(platform_root, "production", "start-gate"),
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line(
            [*prefix, "up", "--no-build", "-d", "--remove-orphans"],
            runtime=runtime,
        ),
    ]


def test_production_existing_generation_runs_redis_tls_guard_before_compose(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    baseline = seed_runtime_generation(runtime)
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    prefix = compose_prefix(platform_root, production=True)
    assert command_lines(log) == [
        expected_release_manager(platform_root, "production", "start-gate"),
        expected_preprocessor(platform_root, runtime, "current-target"),
        expected_prepare(platform_root, runtime, mode="production"),
        expected_redis_tls_rotation_guard(platform_root, runtime, baseline),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", "--no-build", "-d"], runtime=runtime),
    ]


def test_production_redis_tls_guard_failure_reactivates_baseline_without_docker(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    baseline = seed_runtime_generation(runtime)
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_REDIS_TLS_ROTATION_GUARD_EXIT": "41",
        },
    )

    assert result.returncode == 41
    assert "previous generation reactivated" in result.stderr
    lines = command_lines(log)
    assert lines == [
        expected_release_manager(platform_root, "production", "start-gate"),
        expected_preprocessor(platform_root, runtime, "current-target"),
        expected_prepare(platform_root, runtime, mode="production"),
        expected_redis_tls_rotation_guard(platform_root, runtime, baseline),
        expected_preprocessor(
            platform_root,
            runtime,
            "activate",
            "--target",
            baseline,
        ),
    ]
    assert not any(line.startswith("docker|") for line in lines)


def test_production_empty_generation_root_is_explicit_full_stop_path(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    (runtime / "generations").mkdir()
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    lines = command_lines(log)
    assert expected_prepare(platform_root, runtime, mode="production") in lines
    assert not any("redis_tls_rotation_guard.py" in line for line in lines)


def test_production_orphan_generation_without_current_fails_before_prepare_or_docker(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    (runtime / "generations" / "generation-orphan").mkdir(parents=True)
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert "without a valid current pointer" in result.stderr
    lines = command_lines(log)
    assert lines == [expected_release_manager(platform_root, "production", "start-gate")]
    assert not any("prepare_runtime_secrets.py|prepare" in line for line in lines)
    assert not any(line.startswith("docker|") for line in lines)


def test_development_existing_generation_does_not_invoke_production_tls_guard(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    seed_runtime_generation(runtime)

    result = run_wrapper(fake_environment, "up", "-d")

    assert result.returncode == 0, result.stderr
    lines = command_lines(log)
    assert lines[0] == expected_prepare(platform_root, runtime)
    assert not any("current-target" in line for line in lines)
    assert not any("redis_tls_rotation_guard.py" in line for line in lines)


@pytest.mark.parametrize(
    "failure_variable",
    (
        "FAKE_STORAGE_PREFLIGHT_EXIT",
        "FAKE_REDIS_TLS_PREFLIGHT_EXIT",
        "FAKE_VOLUME_PREFLIGHT_EXIT",
    ),
)
def test_production_infrastructure_preflight_failure_prevents_any_mutation(
    fake_environment: tuple[Path, Path, dict[str, str]],
    failure_variable: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            failure_variable: "23",
        },
    )

    assert result.returncode != 0
    assert "preflight failed" in result.stderr
    assert command_lines(log) == []


def test_phase0_production_rejects_unimplemented_managed_mode_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root, redis_ha_mode="managed")

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            "FAKE_REDIS_TLS_PREFLIGHT_EXIT": "23",
        },
    )

    assert result.returncode != 0
    assert "REDIS_HA_MODE is invalid" in result.stderr
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "redis_ha_mode", (None, "standalone", "managed", "unknown", " managed")
)
def test_production_rejects_missing_or_invalid_redis_topology_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    redis_ha_mode: str | None,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, redis_ha_mode=redis_ha_mode)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_production_rejects_duplicate_redis_topology_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)
    with (platform_root / ".env").open("a", encoding="utf-8") as stream:
        stream.write("REDIS_HA_MODE=managed\n")

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize("name", ("REDIS_HA_MODE", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"))
def test_production_rejects_shell_topology_override_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    name: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={
            "SMS_SECRETS_MODE": "production",
            name: "isolated-standalone" if name == "REDIS_HA_MODE" else "override",
        },
    )

    assert result.returncode != 0
    assert command_lines(log) == []


@pytest.mark.parametrize(
    "name",
    (
        "SMS_API_IMAGE",
        "SMS_WEB_IMAGE",
        "SMS_POSTGRES_IMAGE",
        "SMS_REDIS_IMAGE",
        "METRICS_ALLOWED_CIDRS",
    ),
)
@pytest.mark.parametrize("value", ("", "registry.example.invalid/sms:unsealed"))
def test_production_rejects_shell_image_or_metrics_override_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    name: str,
    value: str,
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


@pytest.mark.parametrize(
    "metrics_allowed_cidrs",
    (None, "172.31.250.0/24", "172.16.0.0/12", " 172.31.250.1/32"),
)
def test_production_requires_exact_metrics_source_in_root_env_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
    metrics_allowed_cidrs: str | None,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(
        platform_root,
        metrics_allowed_cidrs=metrics_allowed_cidrs,
    )

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_development_allows_shell_image_and_metrics_overrides(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "ps",
        extra_environment={
            "SMS_API_IMAGE": "sms-api:development",
            "SMS_WEB_IMAGE": "sms-web:development",
            "SMS_POSTGRES_IMAGE": "postgres:development",
            "SMS_REDIS_IMAGE": "redis:development",
            "METRICS_ALLOWED_CIDRS": "172.16.0.0/12",
        },
    )

    assert result.returncode == 0, result.stderr
    assert len(command_lines(log)) == 1


@pytest.mark.parametrize(
    "bind_ip",
    ["0.0.0.0", "::", ".".join(("8", "8", "8", "8")), "10.0.0.5"],
)
def test_production_direct_mode_rejects_remote_plaintext_bind_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], bind_ip: str
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root, web_bind_ip=bind_ip)

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


def test_production_external_tls_bind_requires_private_address_and_proxy_acl(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    write_non_secret_env(
        platform_root,
        web_bind_ip="10.0.0.5",
        external_tls_mode="1",
        trusted_proxy_cidrs="10.0.0.9/32",
    )

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0
    prefix = compose_prefix(platform_root, production=True)
    assert command_lines(log) == [
        expected_release_manager(platform_root, "production", "start-gate"),
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", "--no-build", "-d"], runtime=runtime),
    ]


@pytest.mark.parametrize("cidrs", ["", "10.0.0.0/16", "0.0.0.0/0"])
def test_production_external_tls_rejects_missing_or_broad_proxy_acl_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]], cidrs: str
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(
        platform_root,
        web_bind_ip="10.0.0.5",
        external_tls_mode="1",
        trusted_proxy_cidrs=cidrs,
    )

    result = run_wrapper(
        fake_environment,
        "up",
        "-d",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode != 0
    assert command_lines(log) == []


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
                    "worker-report",
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
    prefix = compose_prefix(platform_root, production=True)
    assert command_lines(log) == [
        expected_release_manager(platform_root, "production", "start-gate"),
        expected_prepare(platform_root, runtime, mode="production"),
        expected_line([*prefix, "config", "--quiet"], runtime=runtime),
        expected_line([*prefix, "up", "--no-build", *arguments], runtime=runtime),
    ]


def test_production_rotate_recreate_forbids_local_build(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "rotate",
        "backend",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    assert any("|up|-d|--no-build|--no-deps|" in line for line in command_lines(log))


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
        external_tls_mode="0",
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


def test_development_up_renders_trusted_proxy_from_root_dotenv(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, _, environment = fake_environment
    write_non_secret_env(
        platform_root,
        environment="development",
        debug="1",
        auth_mock="1",
        vendor_mock="0",
        web_bind_ip="127.0.0.1",
        external_tls_mode="1",
        trusted_proxy_cidrs="127.0.0.1/32,172.31.250.1/32",
    )

    result = run_wrapper(fake_environment, "up", "-d", "web")

    assert result.returncode == 0, result.stderr
    rendered = Path(environment["SMS_TRUSTED_PROXY_CONF"]).read_text(
        encoding="utf-8"
    )
    assert "# Generated by render_trusted_proxy_conf.py (external TLS mode)." in rendered
    assert "set_real_ip_from 127.0.0.1/32;" in rendered
    assert "set_real_ip_from 172.31.250.1/32;" in rendered


def test_development_up_requires_explicit_tls_mode_before_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, _ = fake_environment
    (platform_root / ".env").write_text(
        "ENVIRONMENT=development\n",
        encoding="utf-8",
    )

    result = run_wrapper(fake_environment, "up", "-d")

    assert result.returncode != 0
    assert "requires explicit SMS_EXTERNAL_TLS_MODE" in result.stderr
    assert command_lines(log) == []


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


def test_production_rejects_direct_private_locked_action_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, environment = fake_environment
    runtime = Path(environment["SMS_RUNTIME_ROOT"])
    lock_path = Path(f"{runtime}.lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            [str(WRAPPER), "__locked", "down"],
            env={
                **environment,
                "SMS_SECRETS_MODE": "production",
                "SMS_PRODUCTION_CONTROL_SMOKE": "0",
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
    assert "direct private locked entry is forbidden in production" in result.stderr
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


@pytest.mark.parametrize("argument", ("-v", "--volumes"))
def test_production_down_refuses_persistent_volume_removal(
    fake_environment: tuple[Path, Path, dict[str, str]],
    argument: str,
) -> None:
    platform_root, log, _ = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "down",
        argument,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert "retain persistent volumes" in result.stderr
    assert command_lines(log) == []


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
        ("config", "--output", "/etc/systemd/system/sms-platform.service"),
        ("config", "-o", "/etc/sms-platform/platform.env"),
        ("ps", "--format", "json"),
        ("logs", "--follow"),
        ("logs", "api"),
    ],
)
def test_production_diagnostics_reject_all_arguments_before_docker(
    fake_environment: tuple[Path, Path, dict[str, str]],
    arguments: tuple[str, ...],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        *arguments,
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert "production diagnostics do not accept arguments" in result.stderr
    assert command_lines(log) == []


def test_production_config_allows_only_the_existing_quiet_probe(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    platform_root, log, environment = fake_environment
    write_non_secret_env(platform_root)

    result = run_wrapper(
        fake_environment,
        "config",
        "--quiet",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 0, result.stderr
    assert command_lines(log) == [
        expected_line(
            [*compose_prefix(platform_root, production=True), "config", "--quiet"],
            runtime=Path(environment["SMS_RUNTIME_ROOT"]),
        )
    ]


def test_production_rejects_generic_exec_before_any_tool_call(
    fake_environment: tuple[Path, Path, dict[str, str]],
) -> None:
    _, log, _ = fake_environment

    result = run_wrapper(
        fake_environment,
        "exec",
        "api",
        "sh",
        extra_environment={"SMS_SECRETS_MODE": "production"},
    )

    assert result.returncode == 2
    assert "generic exec is forbidden" in result.stderr
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
    prefix = compose_prefix(platform_root, production=True)
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
        expected_release_manager(platform_root, "production", "start-gate"),
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
    prefix = compose_prefix(platform_root, production=True)
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
        expected_release_manager(platform_root, "production", "start-gate"),
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
                "worker-report",
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
        "worker-report",
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
