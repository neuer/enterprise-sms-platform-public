from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local_test.sh"
SECRET_NAMES = {
    "vendor_secret_name",
    "vendor_secret_key",
    "data_aes_key",
    "data_hmac_key",
    "jwt_secret",
    "ldap_bind_password",
    "metrics_scrape_token",
    "db_owner_password",
    "db_auth_password",
    "db_accept_password",
    "db_send_password",
    "db_callback_password",
    "db_export_password",
    "db_scheduler_password",
    "db_metrics_password",
    "redis_broker_password",
    "redis_auth_password",
    "redis_control_password",
}
PORT_OVERRIDE_NAMES = {"WEB_PORT", "API_PORT", "MOCK_VENDOR_PORT"}


def clean_test_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """隔离调用方端口覆盖，确保默认值测试可在共享 G2 环境复现。"""

    environment = {
        name: value for name, value in os.environ.items() if name not in PORT_OVERRIDE_NAMES
    }
    environment.update(overrides or {})
    return environment


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=clean_test_environment(env),
        check=False,
        capture_output=True,
        text=True,
    )


def source_call(command: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {command}'],
        cwd=ROOT,
        env=clean_test_environment(env),
        check=False,
        capture_output=True,
        text=True,
    )


def test_help_documents_commands_ports_and_mock_accounts() -> None:
    result = run_script("help")

    assert result.returncode == 0
    assert "prepare|up|status|down|reset|help" in result.stdout
    assert "http://localhost:18180" in result.stdout
    assert "http://localhost:18100" in result.stdout
    assert "http://localhost:19128" in result.stdout
    for username in ("admin01", "approver01", "operator01", "viewer01"):
        assert username in result.stdout
    assert "ldap_bind_password" in result.stdout
    assert "统一密码" not in result.stdout


def test_help_does_not_require_docker_or_start_public_session(tmp_path: Path) -> None:
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    result = run_script(
        "help",
        env={"PATH": f"{empty_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "prepare|up|status|down|reset|help" in result.stdout
    assert "Docker CLI is unavailable" not in result.stderr


def test_prepare_creates_only_dev_configuration_and_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    secrets_dir = tmp_path / "secrets"

    result = run_script(
        "prepare",
        env={
            "LOCAL_ENV_FILE": str(env_file),
            "LOCAL_SECRETS_DIR": str(secrets_dir),
        },
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert {item.name for item in secrets_dir.iterdir()} == SECRET_NAMES
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in secrets_dir.iterdir())
    for secret in secrets_dir.iterdir():
        assert secret.read_text(encoding="utf-8").strip() not in result.stdout
        assert secret.read_text(encoding="utf-8").strip() not in result.stderr


def test_invalid_command_is_rejected() -> None:
    result = run_script("unknown")

    assert result.returncode == 2
    assert "不支持的命令" in result.stderr


def test_dev_environment_validation_rejects_non_mock_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ENVIRONMENT=development\nDEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=0\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\n",
        encoding="utf-8",
    )

    result = source_call("validate_dev_env", env={"LOCAL_ENV_FILE": str(env_file)})

    assert result.returncode != 0
    assert "VENDOR_MOCK=1" in result.stderr


def test_dev_environment_validation_accepts_only_local_mock(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ENVIRONMENT=development\nDEBUG=1 # dev\nAUTH_MOCK=1\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\n",
        encoding="utf-8",
    )

    result = source_call("validate_dev_env", env={"LOCAL_ENV_FILE": str(env_file)})

    assert result.returncode == 0, result.stderr


def test_dev_secrets_are_0600_and_existing_values_are_not_overwritten(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    existing = secrets_dir / "vendor_secret_name"
    existing.write_text("keep-me", encoding="utf-8")
    existing.chmod(0o600)

    result = source_call(
        "ensure_dev_secrets; ensure_dev_secrets",
        env={"LOCAL_SECRETS_DIR": str(secrets_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert existing.read_text(encoding="utf-8") == "keep-me"
    assert {item.name for item in secrets_dir.iterdir()} == SECRET_NAMES
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in secrets_dir.iterdir())


def test_legacy_short_mock_password_is_rotated_but_safe_random_value_is_preserved(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    password_file = secrets_dir / "ldap_bind_password"
    password_file.write_text("legacy-short", encoding="utf-8")
    password_file.chmod(0o600)

    first = source_call(
        "ensure_dev_secrets",
        env={"LOCAL_SECRETS_DIR": str(secrets_dir)},
    )
    assert first.returncode == 0, first.stderr
    generated = password_file.read_text(encoding="utf-8").rstrip()
    assert len(generated) >= 32
    assert "legacy-short" not in first.stdout + first.stderr

    second = source_call(
        "ensure_dev_secrets",
        env={"LOCAL_SECRETS_DIR": str(secrets_dir)},
    )
    assert second.returncode == 0, second.stderr
    assert password_file.read_text(encoding="utf-8").rstrip() == generated


def test_script_has_bounded_health_seed_and_dev_profile_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"$ROOT/deploy/sms-compose" "$@"' in source
    assert 'SMS_PLATFORM_ROOT="$ROOT"' in source
    assert "SMS_SECRETS_MODE=development" in source
    assert (
        'SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/'
        'sms-platform-${UID}/secrets}"' in source
    )
    assert "COMPOSE_PROFILES=dev" in source
    assert "--profile dev" not in source
    assert 'docker compose -f "$COMPOSE_FILE"' not in source
    assert "LOCAL_HEALTH_ATTEMPTS" in source
    assert "python -m app.cli seed-dev" in source
    assert "dev-apikeys.txt" in source
    assert "'exec dd if=\"$1\" status=none'" in source
    assert "'umask 077; exec dd of=\"$1\" status=none'" in source
    assert "compose cp" not in source
    assert "down -v" in source
    assert 'http://localhost:${MOCK_VENDOR_PORT}/_mock/state' in source
    assert "/__mock/state" not in source
    assert "cat $" not in source
