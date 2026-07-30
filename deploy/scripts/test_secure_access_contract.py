"""开发测试临时 HTTPS 入口的固定、无敏感数据合同。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CLOUDFLARED_VERSION = "2026.7.2"
CLOUDFLARED_SHA256 = (
    "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd"
)
CLOUDFLARED_PATH = Path("/usr/local/libexec/sms-platform/cloudflared")
SERVICE_NAME = "sms-platform-test-secure-access.service"
ORIGIN = "http://127.0.0.1:18080"
STATUS_PATH = Path("/run/sms-platform-test-secure-access/status.json")
TEST_HOST_MARKER_PATH = Path("/etc/sms-platform/test-host")
MAX_LIFETIME_SECONDS = 900
HOST_ASSET_ROOT = Path("/usr/local/libexec/sms-platform/test-secure-access")
HOST_MANIFEST_PATH = HOST_ASSET_ROOT / "manifest.json"
HOST_CONTROL_SOURCE_ASSETS = (
    ("install_test_secure_access.py", "deploy/scripts/install_test_secure_access.py"),
    ("test_secure_access_contract.py", "deploy/scripts/test_secure_access_contract.py"),
    ("test_secure_access_runtime.py", "deploy/scripts/test_secure_access_runtime.py"),
    ("test_secure_access_manager.py", "deploy/scripts/test_secure_access_manager.py"),
    ("vendor_test_files.py", "deploy/scripts/vendor_test_files.py"),
    ("check_test_update_migration.py", "deploy/scripts/check_test_update_migration.py"),
    ("run_with_lifecycle_lock.py", "deploy/scripts/run_with_lifecycle_lock.py"),
    (
        "public_cutover_bootstrap.py",
        "deploy/scripts/public_cutover_bootstrap.py",
    ),
    ("test_update_apply.py", "deploy/scripts/test_update_apply.py"),
    ("test_update_backup.py", "deploy/scripts/test_update_backup.py"),
    ("test_update_contract.py", "deploy/scripts/test_update_contract.py"),
    ("test_update_manager.py", "deploy/scripts/test_update_manager.py"),
    ("test_update_store.py", "deploy/scripts/test_update_store.py"),
    ("test_update_verify.py", "deploy/scripts/test_update_verify.py"),
    ("check_public_readiness.py", "scripts/check_public_readiness.py"),
    ("export_public_snapshot.py", "scripts/export_public_snapshot.py"),
    (
        "verify_public_snapshot_cutover.py",
        "scripts/verify_public_snapshot_cutover.py",
    ),
    ("sms-compose-bootstrap", "deploy/sms-compose"),
    (SERVICE_NAME, f"deploy/systemd/{SERVICE_NAME}"),
)
HOST_CONTROL_SOURCE_PATHS = tuple(path for _name, path in HOST_CONTROL_SOURCE_ASSETS)
HOST_ASSET_NAMES = (
    *(name for name, _path in HOST_CONTROL_SOURCE_ASSETS),
    "cloudflared",
)

_URL_RE = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?[.]trycloudflare[.]com"
)
_STATE_FIELDS = {
    "schema_version",
    "status",
    "url",
    "started_at",
    "expires_at",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_TEST_HOST_MARKER = {
    "schema_version": 1,
    "mode": "development-test-host",
    "purpose": "temporary-https-and-fast-update",
}


class SecureAccessContractError(ValueError):
    """临时 HTTPS 运行态元数据不符合固定合同。"""


@dataclass(frozen=True, slots=True)
class SecureAccessState:
    """一条已就绪 Quick Tunnel 的无敏感运行态。"""

    url: str
    started_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HostManifest:
    """root-owned 主机资产及其来源 commit。"""

    source_commit: str
    files: Mapping[str, str]


def parse_quick_tunnel_url(value: object) -> str:
    """只接受无 path/query/fragment 的 Cloudflare Quick Tunnel HTTPS URL。"""

    if type(value) is not str or _URL_RE.fullmatch(value) is None:
        raise SecureAccessContractError("secure access URL is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise SecureAccessContractError("secure access state has duplicate fields")
        document[key] = value
    return document


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise SecureAccessContractError("secure access state has invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SecureAccessContractError(
            "secure access state has invalid timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SecureAccessContractError("secure access state has invalid timestamp")
    return parsed


def _validate_window(started_at: datetime, expires_at: datetime) -> None:
    if (
        started_at.tzinfo is None
        or started_at.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise SecureAccessContractError("secure access state has invalid timestamp")
    lifetime = expires_at.astimezone(UTC) - started_at.astimezone(UTC)
    if lifetime <= timedelta(0) or lifetime > timedelta(
        seconds=MAX_LIFETIME_SECONDS
    ):
        raise SecureAccessContractError("secure access state has invalid lifetime")


def serialize_ready_state(
    *,
    url: str,
    started_at: datetime,
    expires_at: datetime,
) -> str:
    """生成固定字段、固定时限的 ready JSON。"""

    safe_url = parse_quick_tunnel_url(url)
    _validate_window(started_at, expires_at)
    document = {
        "schema_version": 1,
        "status": "ready",
        "url": safe_url,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"


def parse_ready_state(raw: object) -> SecureAccessState:
    """严格解析运行器写入的 ready JSON。"""

    if type(raw) is not str:
        raise SecureAccessContractError("secure access state is invalid")
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, SecureAccessContractError) as exc:
        raise SecureAccessContractError("secure access state is invalid") from exc
    if type(document) is not dict or set(document) != _STATE_FIELDS:
        raise SecureAccessContractError("secure access state has invalid fields")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["status"] != "ready"
    ):
        raise SecureAccessContractError("secure access state has invalid values")
    url = parse_quick_tunnel_url(document["url"])
    started_at = _parse_timestamp(document["started_at"])
    expires_at = _parse_timestamp(document["expires_at"])
    _validate_window(started_at, expires_at)
    return SecureAccessState(
        url=url,
        started_at=started_at,
        expires_at=expires_at,
    )


def _validate_host_digests(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != set(HOST_ASSET_NAMES):
        raise SecureAccessContractError("secure access host manifest is invalid")
    files = value
    if any(
        type(name) is not str
        or type(digest) is not str
        or _SHA256_RE.fullmatch(digest) is None
        for name, digest in files.items()
    ):
        raise SecureAccessContractError("secure access host manifest is invalid")
    return dict(files)


def serialize_host_manifest(
    digests: Mapping[str, str],
    *,
    source_commit: str,
) -> str:
    """生成 root-owned 主机资产精确哈希清单。"""

    files = _validate_host_digests(dict(digests))
    if type(source_commit) is not str or _COMMIT_RE.fullmatch(source_commit) is None:
        raise SecureAccessContractError("secure access host manifest is invalid")
    return json.dumps(
        {
            "schema_version": 1,
            "source_commit": source_commit,
            "files": files,
        },
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def parse_host_manifest(raw: object) -> HostManifest:
    """严格解析 root-owned 主机资产清单。"""

    if type(raw) is not str:
        raise SecureAccessContractError("secure access host manifest is invalid")
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, SecureAccessContractError) as exc:
        raise SecureAccessContractError(
            "secure access host manifest is invalid"
        ) from exc
    if (
        type(document) is not dict
        or set(document) != {"schema_version", "source_commit", "files"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or type(document["source_commit"]) is not str
        or _COMMIT_RE.fullmatch(document["source_commit"]) is None
    ):
        raise SecureAccessContractError("secure access host manifest is invalid")
    return HostManifest(
        source_commit=document["source_commit"],
        files=_validate_host_digests(document["files"]),
    )


def serialize_test_host_marker() -> str:
    """生成与 live activation 完全独立的固定测试主机标记。"""

    return json.dumps(
        _TEST_HOST_MARKER,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def parse_test_host_marker(raw: object) -> None:
    """严格验证测试主机标记，不接受 live activation marker。"""

    if type(raw) is not str:
        raise SecureAccessContractError("secure access test host marker is invalid")
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, SecureAccessContractError) as exc:
        raise SecureAccessContractError(
            "secure access test host marker is invalid"
        ) from exc
    if type(document) is not dict or document != _TEST_HOST_MARKER:
        raise SecureAccessContractError("secure access test host marker is invalid")
