from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import vendor_test_files as files  # noqa: E402


def _marker() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "development-vendor-live",
        "vendor_origin": "https://vendor.example.invalid",
        "daily_segment_limit": 100,
        "timezone": "Asia/Shanghai",
        "backup_config": "/etc/sms-platform/test-update-backup.json",
    }


def _write_marker(path: Path, payload: dict[str, object] | None = None) -> None:
    path.write_text(json.dumps(payload or _marker()) + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_marker_accepts_only_exact_root_control_contract(tmp_path: Path) -> None:
    path = tmp_path / "test-environment"
    _write_marker(path)

    marker = files.read_vendor_test_marker(path, expected_uid=os.geteuid())

    assert marker.mode == "development-vendor-live"
    assert marker.daily_segment_limit == 100
    assert not hasattr(marker, "allowlist_file")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor_origin", "http://vendor.example.invalid"),
        ("daily_segment_limit", 101),
        ("timezone", "UTC"),
        ("backup_config", "/tmp/backup.json"),
    ],
)
def test_marker_rejects_any_fixed_contract_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _marker()
    payload[field] = value
    path = tmp_path / "test-environment"
    _write_marker(path, payload)

    with pytest.raises(files.VendorTestFileError, match="marker contract"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid())


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", True), ("daily_segment_limit", True)],
)
def test_marker_rejects_boolean_values_for_integer_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _marker()
    payload[field] = value
    path = tmp_path / "test-environment"
    _write_marker(path, payload)

    with pytest.raises(files.VendorTestFileError, match="marker contract"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid())


def test_marker_rejects_unknown_fields_symlink_mode_and_owner(tmp_path: Path) -> None:
    path = tmp_path / "test-environment"
    payload = _marker()
    payload["secret_path"] = "/run/secrets/vendor_secret_key"
    _write_marker(path, payload)
    with pytest.raises(files.VendorTestFileError, match="fields"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid())

    _write_marker(path)
    path.chmod(0o640)
    with pytest.raises(files.VendorTestFileError, match="mode"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid())

    path.chmod(0o600)
    with pytest.raises(files.VendorTestFileError, match="owner"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid() + 1)

    target = tmp_path / "real-marker"
    path.replace(target)
    path.symlink_to(target)
    with pytest.raises(files.VendorTestFileError, match="regular file"):
        files.read_vendor_test_marker(path, expected_uid=os.geteuid())


def test_live_allowlist_requires_backend_group_read_access(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.json"
    path.write_text('{"schema_version":1,"entries":[]}\n', encoding="utf-8")
    path.chmod(0o640)

    assert files.read_vendor_test_allowlist_count(
        path,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    ) == 0

    path.chmod(0o600)
    with pytest.raises(files.VendorTestFileError, match="mode"):
        files.read_vendor_test_allowlist_count(
            path,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )


def test_pure_mock_dotenv_requires_exact_pre_activation_controls(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# retained image pins\n"
        "ENVIRONMENT=development\nDEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\nCOMPOSE_PROFILES=dev\n"
        "SMS_API_IMAGE=registry/api@sha256:abc\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    files.require_pure_mock_dotenv(path, expected_uid=os.geteuid())

    path.write_text(
        path.read_text(encoding="utf-8").replace("VENDOR_MOCK=1", "VENDOR_MOCK=0"),
        encoding="utf-8",
    )
    with pytest.raises(files.VendorTestFileError, match="pure Mock"):
        files.require_pure_mock_dotenv(path, expected_uid=os.geteuid())


def test_reconcile_pure_mock_dotenv_removes_only_retired_keys_and_inline_comments(
    tmp_path: Path,
) -> None:
    assert frozenset(
        {
            "BOOTSTRAP_ADMIN_USERS",
            "LDAP_SERVER",
            "LDAP_BASE_DN",
            "LDAP_BIND_DN",
            "LDAP_USER_SEARCH_FILTER",
            "LDAP_CONNECT_TIMEOUT_S",
            "LDAP_RECEIVE_TIMEOUT_S",
        }
    ) == files.RETIRED_PROVIDER_DOTENV_KEYS
    path = tmp_path / ".env"
    path.write_text(
        "# retained\n"
        "SMS_API_IMAGE=registry/api@sha256:abc\n"
        "ENVIRONMENT=development\n"
        "VENDOR_MOCK=1   # mock only\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028   # mock endpoint\n"
        "AUTH_MOCK=1   # local auth\n"
        "DEBUG=1   # development\n"
        "COMPOSE_PROFILES=dev\n"
        "LDAP_SERVER=ldap.example.test\n"
        "LDAP_BASE_DN=dc=example,dc=test\n"
        "LDAP_BIND_DN=cn=reader,dc=example,dc=test\n"
        "LDAP_USER_SEARCH_FILTER=(uid={username})\n"
        "LDAP_CONNECT_TIMEOUT_S=5\n"
        "LDAP_RECEIVE_TIMEOUT_S=10\n"
        "BOOTSTRAP_ADMIN_USERS=legacy.admin,other.admin\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert files.reconcile_pure_mock_dotenv(path, expected_uid=os.geteuid()) is True

    rendered = path.read_text(encoding="utf-8")
    assert "SMS_API_IMAGE=registry/api@sha256:abc" in rendered
    assert "VENDOR_MOCK=1\n" in rendered
    assert "VENDOR_BASE_URL=http://mock-vendor:9028\n" in rendered
    assert "AUTH_MOCK=1\n" in rendered
    assert "DEBUG=1\n" in rendered
    assert "ENVIRONMENT=development\n" in rendered
    assert "# mock only" not in rendered
    for retired in files.RETIRED_PROVIDER_DOTENV_KEYS:
        assert f"{retired}=" not in rendered
    files.require_pure_mock_dotenv(path, expected_uid=os.geteuid())

    before = path.read_bytes()
    assert files.reconcile_pure_mock_dotenv(path, expected_uid=os.geteuid()) is False
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "unsafe_line",
    [
        "FUTURE_SETTING=value with spaces",
        "VENDOR_MOCK=0   # production mode",
        "VENDOR_SECRET_NAME=must-not-be-in-dotenv",
    ],
)
def test_reconcile_pure_mock_dotenv_rejects_unknown_or_non_mock_values_without_write(
    tmp_path: Path,
    unsafe_line: str,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "ENVIRONMENT=development\nDEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\nCOMPOSE_PROFILES=dev\n"
        f"{unsafe_line}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    before = path.read_bytes()

    with pytest.raises(files.VendorTestFileError):
        files.reconcile_pure_mock_dotenv(path, expected_uid=os.geteuid())

    assert path.read_bytes() == before


def test_reconcile_pure_mock_dotenv_rejects_duplicate_retired_key_without_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "ENVIRONMENT=development\nDEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\nCOMPOSE_PROFILES=dev\n"
        "LDAP_BASE_DN=dc=first,dc=test\n"
        "LDAP_BASE_DN=dc=second,dc=test\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    before = path.read_bytes()

    with pytest.raises(files.VendorTestFileError, match="duplicate"):
        files.reconcile_pure_mock_dotenv(path, expected_uid=os.geteuid())

    assert path.read_bytes() == before


def test_development_dotenv_example_is_strict_pure_mock_input(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes((ROOT / "deploy/.env.example").read_bytes())
    path.chmod(0o600)

    files.require_pure_mock_dotenv(path, expected_uid=os.geteuid())


def test_dotenv_update_changes_only_fixed_live_keys_and_preserves_images(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# keep\n"
        "DEBUG=0\nAUTH_MOCK=0\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\nCOMPOSE_PROFILES=dev\n"
        "SMS_API_IMAGE=registry/api@sha256:abc\nPOSTGRES_DB=sms\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    files.activate_vendor_live_dotenv(path, expected_uid=os.geteuid())

    rendered = path.read_text(encoding="utf-8")
    assert "ENVIRONMENT=development" in rendered
    assert "DEBUG=1" in rendered
    assert "AUTH_MOCK=1" in rendered
    assert "VENDOR_MOCK=0" in rendered
    assert "VENDOR_BASE_URL=https://vendor.example.invalid" in rendered
    assert "COMPOSE_PROFILES=\n" in rendered
    assert (
        "SMS_VENDOR_TEST_STATE_DIR=/var/lib/sms-platform/vendor-test" in rendered
    )
    assert (
        "SMS_VENDOR_CONTROL_SOCKET_DIR=/run/sms-platform/vendor-control" in rendered
    )
    assert "SMS_API_IMAGE=registry/api@sha256:abc" in rendered
    assert "POSTGRES_DB=sms" in rendered
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "raw",
    [
        "DEBUG=0\nDEBUG=1\n",
        "DEBUG=$(id)\n",
        "DEBUG=`id`\n",
        "export DEBUG=1\n",
        "VENDOR_SECRET_KEY=leak\n",
        "vendor_secret_name=leak\n",
        "DEBUG='1'\n",
    ],
)
def test_dotenv_update_rejects_duplicates_expansion_credentials_and_non_strict_syntax(
    tmp_path: Path,
    raw: str,
) -> None:
    path = tmp_path / ".env"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(files.VendorTestFileError):
        files.activate_vendor_live_dotenv(path, expected_uid=os.geteuid())


def test_pii_free_evidence_rejects_sensitive_field_names_and_values() -> None:
    with pytest.raises(files.VendorTestFileError, match="evidence"):
        files.require_pii_free_evidence({"phone": "13800138000"})
    with pytest.raises(files.VendorTestFileError, match="evidence"):
        files.require_pii_free_evidence({"detail": "recipient=13800138000"})
    with pytest.raises(files.VendorTestFileError, match="evidence"):
        files.require_pii_free_evidence({"secret_hash": "abc"})

    files.require_pii_free_evidence(
        {"status": "blocked", "vendor_code": 1010, "recipient_count": 1}
    )
