from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local_test.sh"
SECRET_NAMES = {
    "vendor_secret_name",
    "vendor_secret_key",
    "data_aes_key",
    "data_hmac_key",
    "api_key_pepper_key",
    "audit_context_key",
    "audit_system_api_context_key",
    "audit_system_realtime_context_key",
    "audit_system_bulk_context_key",
    "alert_credential_public_key",
    "alert_credential_private_key",
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
    "redis_tls_server_key",
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
    assert (
        "METRICS_ALLOWED_CIDRS=127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
        in env_file.read_text(encoding="utf-8")
    )
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert {item.name for item in secrets_dir.iterdir()} == SECRET_NAMES
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in secrets_dir.iterdir())
    for secret in secrets_dir.iterdir():
        assert secret.read_text(encoding="utf-8").strip() not in result.stdout
        assert secret.read_text(encoding="utf-8").strip() not in result.stderr
    private = X25519PrivateKey.from_private_bytes(
        base64.b64decode(
            (secrets_dir / "alert_credential_private_key").read_text().strip()
        )
    )
    expected_public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    assert base64.b64decode(
        (secrets_dir / "alert_credential_public_key").read_text().strip()
    ) == expected_public


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
    assert (
        'SMS_TRUSTED_PROXY_CONF="${SMS_TRUSTED_PROXY_CONF:-$ROOT/.test-update/trusted-proxies.conf}"'
        in source
    )
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


def startup_trace(tmp_path: Path, *, replies: list[tuple[int, str]], fail_at: str = ""):
    """执行真实 up 控制流，仅把外部服务替换成可观测适配器。"""
    import json

    trace = tmp_path / "trace"
    response = tmp_path / "responses.json"
    response.write_text(json.dumps(replies), encoding="utf-8")
    index = tmp_path / "index"
    result = source_call(
        """
record() { printf '%s\\n' "$*" >> "$TRACE"; [[ "$*" != "$FAIL_AT" ]]; }
require_commands() { record commands; }
ensure_dev_env() { record env; }
validate_dev_env() { record validate; }
ensure_dev_secrets() { record secrets; }
compose() {
  record "compose $*" || return $?
  if [[ "$1" == test-update ]]; then
    python3 - "$REPLIES" "$INDEX" <<'PYTHON'
import json, sys
from pathlib import Path
replies = json.loads(Path(sys.argv[1]).read_text())
index = Path(sys.argv[2])
position = int(index.read_text()) if index.exists() else 0
status, reply = replies[min(position, len(replies) - 1)]
index.write_text(str(position + 1))
print(reply)
raise SystemExit(status)
PYTHON
  fi
}
sleep() { record "sleep $*"; }
wait_url() { record "health $1"; }
check_mock_catalog() { record catalog; }
seed_dev() { record seed; }
print_ready() { record ready; }
up
""",
        env={
            "TRACE": str(trace),
            "REPLIES": str(response),
            "INDEX": str(index),
            "FAIL_AT": fail_at,
        },
    )
    return result, trace.read_text().splitlines()


def cutover_reply(state: str, *, error: str = "", admission: str = "open", ok: bool = True):
    import json

    return json.dumps({"ok": ok, "state": state, "error": error, "admission_state": admission})


def test_up_finishes_controlled_cutover_before_api_restart_seed_and_ready(tmp_path: Path) -> None:
    waiting = cutover_reply("waiting_window", error="waiting for not_before", admission="closed")
    active = cutover_reply("active_v2")
    result, calls = startup_trace(tmp_path, replies=[(1, waiting), (0, active)])
    assert result.returncode == 0, result.stderr
    assert calls == [
        "commands",
        "env",
        "validate",
        "secrets",
        "compose up -d --build",
        "compose test-update writer-cutover",
        "sleep 5",
        "compose test-update writer-cutover",
        "compose up -d api",
        "health API",
        "health Mock vendor",
        "catalog",
        "seed",
        "ready",
    ]
    assert not any("down" in call or "HSET" in call for call in calls)


def test_up_reentry_with_active_writer_does_not_wait(tmp_path: Path) -> None:
    result, calls = startup_trace(tmp_path, replies=[(0, cutover_reply("active_v2"))])
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("sleep") for call in calls)
    assert calls[-1] == "ready"


@pytest.mark.parametrize(
    "status,reply",
    [
        (1, ""),
        (1, "not-json"),
        (
            1,
            cutover_reply(
                "preparing", error="cutover marker conflict", admission="closed", ok=False
            ),
        ),
        (1, cutover_reply("waiting_window", error="waiting for not_before", admission="open")),
        (0, cutover_reply("waiting_window", error="waiting for not_before", admission="closed")),
        (0, cutover_reply("active_v2", admission="closed")),
        (0, cutover_reply("active_v2", ok=False)),
    ],
)
def test_up_cutover_failure_never_restarts_seeds_or_claims_ready(tmp_path: Path, status, reply):
    result, calls = startup_trace(tmp_path, replies=[(status, reply)])
    assert result.returncode != 0
    assert calls[-1] == "compose test-update writer-cutover"
    assert "seed" not in calls and "ready" not in calls


def test_up_cutover_wait_is_bounded(tmp_path: Path) -> None:
    waiting = cutover_reply("waiting_window", error="waiting for not_before", admission="closed")
    result, calls = startup_trace(tmp_path, replies=[(1, waiting)])
    assert result.returncode != 0
    assert "窗口等待超时" in result.stderr
    assert calls.count("compose test-update writer-cutover") == 16
    assert calls.count("sleep 5") == 15
    assert "seed" not in calls and "ready" not in calls


@pytest.mark.parametrize(
    "failure", ["compose up -d api", "health API", "health Mock vendor", "seed"]
)
def test_up_failure_after_cutover_does_not_claim_ready(tmp_path: Path, failure: str) -> None:
    result, calls = startup_trace(
        tmp_path, replies=[(0, cutover_reply("active_v2"))], fail_at=failure
    )
    assert result.returncode != 0
    assert calls[-1] == failure
    assert "ready" not in calls
