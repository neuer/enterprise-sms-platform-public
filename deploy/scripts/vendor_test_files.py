#!/usr/bin/env python3
"""真实厂商联调 marker、dotenv 与无 PII evidence 文件合同。"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

VENDOR_ORIGIN = os.environ.get(
    "SMS_VENDOR_LIVE_TEST_ORIGIN",
    "https://vendor.example.invalid",
)
VENDOR_TEST_MODE = "development-vendor-live"
DAILY_SEGMENT_LIMIT = 100
TIMEZONE = "Asia/Shanghai"
BACKUP_CONFIG_FILE = Path("/etc/sms-platform/test-update-backup.json")
LIVE_DOTENV_VALUES: Mapping[str, str] = {
    "ENVIRONMENT": "development",
    "DEBUG": "1",
    "AUTH_MOCK": "1",
    "VENDOR_MOCK": "0",
    "VENDOR_BASE_URL": VENDOR_ORIGIN,
    "VENDOR_LIVE_TEST_ORIGIN": VENDOR_ORIGIN,
    "COMPOSE_PROFILES": "",
    "SMS_VENDOR_TEST_STATE_DIR": "/var/lib/sms-platform/vendor-test",
    "SMS_VENDOR_CONTROL_SOCKET_DIR": "/run/sms-platform/vendor-control",
}
PURE_MOCK_DOTENV_VALUES: Mapping[str, str] = {
    "ENVIRONMENT": "development",
    "DEBUG": "1",
    "AUTH_MOCK": "1",
    "VENDOR_MOCK": "1",
    "VENDOR_BASE_URL": "http://mock-vendor:9028",
    "COMPOSE_PROFILES": "dev",
}
RETIRED_PROVIDER_DOTENV_KEYS = frozenset(
    {
        "BOOTSTRAP_ADMIN_USERS",
        "LDAP_SERVER",
        "LDAP_BASE_DN",
        "LDAP_BIND_DN",
        "LDAP_USER_SEARCH_FILTER",
        "LDAP_CONNECT_TIMEOUT_S",
        "LDAP_RECEIVE_TIMEOUT_S",
    }
)

_MARKER_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "vendor_origin",
        "daily_segment_limit",
        "timezone",
        "backup_config",
    }
)
_DOTENV_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:@+-]*)")
_DOTENV_INLINE_COMMENT_LINE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:@+-]*)[ \t]+#[^\r\n]*"
)
_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_HMAC64 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_EVIDENCE_KEY = re.compile(
    r"phone|mobile|hmac|secret|password|credential|content|digest|hash|token",
    re.IGNORECASE,
)
_VENDOR_CREDENTIAL_KEYS = frozenset(
    {"VENDOR_SECRET_NAME", "VENDOR_SECRET_KEY", "SECRETNAME", "SECRETKEY"}
)
_ALLOWLIST_FIELDS = frozenset({"schema_version", "entries"})
_ALLOWLIST_ENTRY_FIELDS = frozenset({"key_version", "phone_hmac"})
_MAX_KEY_VERSION = 32767


class VendorTestFileError(ValueError):
    """受控联调文件不符合固定安全合同。"""


@dataclass(frozen=True, slots=True)
class VendorTestMarker:
    schema_version: int
    mode: str
    vendor_origin: str
    daily_segment_limit: int
    timezone: str
    backup_config: Path


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise VendorTestFileError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _validate_private_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int | None = None,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VendorTestFileError("controlled file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise VendorTestFileError("controlled file must be a regular file")
    if metadata.st_uid != expected_uid:
        raise VendorTestFileError("controlled file owner is invalid")
    allowed_modes = {0o640} if expected_gid is not None else {0o400, 0o600}
    if stat.S_IMODE(metadata.st_mode) not in allowed_modes:
        raise VendorTestFileError("controlled file mode is invalid")
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise VendorTestFileError("controlled file group is invalid")


def _atomic_replace(path: Path, payload: bytes, *, expected_uid: int) -> None:
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise VendorTestFileError("controlled file parent is unavailable") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise VendorTestFileError("controlled file parent is unsafe")
    if parent_info.st_uid != expected_uid:
        raise VendorTestFileError("controlled file parent owner is invalid")
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _marker_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": VENDOR_TEST_MODE,
        "vendor_origin": VENDOR_ORIGIN,
        "daily_segment_limit": DAILY_SEGMENT_LIMIT,
        "timezone": TIMEZONE,
        "backup_config": str(BACKUP_CONFIG_FILE),
    }


def read_vendor_test_marker(path: Path, *, expected_uid: int = 0) -> VendorTestMarker:
    """读取 root marker，任何字段或固定值漂移均 fail closed。"""

    _validate_private_file(path, expected_uid=expected_uid)
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except VendorTestFileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VendorTestFileError("marker contains invalid JSON") from exc
    if type(decoded) is not dict:
        raise VendorTestFileError("marker must be an object")
    payload = cast(dict[str, object], decoded)
    if set(payload) != set(_MARKER_FIELDS):
        raise VendorTestFileError("marker fields are invalid")
    if type(payload["schema_version"]) is not int or type(
        payload["daily_segment_limit"]
    ) is not int:
        raise VendorTestFileError("marker contract values are invalid")
    if any(
        type(payload[field]) is not str
        for field in _MARKER_FIELDS - {"schema_version", "daily_segment_limit"}
    ):
        raise VendorTestFileError("marker contract values are invalid")
    if payload != _marker_payload():
        raise VendorTestFileError("marker contract values are invalid")
    return VendorTestMarker(
        1,
        VENDOR_TEST_MODE,
        VENDOR_ORIGIN,
        DAILY_SEGMENT_LIMIT,
        TIMEZONE,
        BACKUP_CONFIG_FILE,
    )


def write_vendor_test_marker(path: Path, *, expected_uid: int = 0) -> VendorTestMarker:
    payload = _marker_payload()
    _atomic_replace(
        path,
        (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        expected_uid=expected_uid,
    )
    return read_vendor_test_marker(path, expected_uid=expected_uid)


def read_vendor_test_allowlist_count(
    path: Path,
    *,
    expected_uid: int = 0,
    expected_gid: int | None = None,
) -> int:
    """只返回严格 root 控制白名单的条目数，不向调用方暴露 HMAC。"""

    _validate_private_file(
        path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    try:
        if path.stat().st_size > 1_048_576:
            raise VendorTestFileError("allowlist is too large")
        decoded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except VendorTestFileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VendorTestFileError("allowlist contains invalid JSON") from exc
    if type(decoded) is not dict:
        raise VendorTestFileError("allowlist must be an object")
    document = cast(dict[str, object], decoded)
    if set(document) != set(_ALLOWLIST_FIELDS):
        raise VendorTestFileError("allowlist fields are invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise VendorTestFileError("allowlist schema version is invalid")
    if type(document["entries"]) is not list:
        raise VendorTestFileError("allowlist entries are invalid")

    seen: set[tuple[int, str]] = set()
    for raw_entry in cast(list[object], document["entries"]):
        if type(raw_entry) is not dict:
            raise VendorTestFileError("allowlist entry is invalid")
        entry = cast(dict[str, object], raw_entry)
        if set(entry) != set(_ALLOWLIST_ENTRY_FIELDS):
            raise VendorTestFileError("allowlist entry fields are invalid")
        version = entry["key_version"]
        digest = entry["phone_hmac"]
        if type(version) is not int or not 1 <= version <= _MAX_KEY_VERSION:
            raise VendorTestFileError("allowlist key version is invalid")
        if type(digest) is not str or _HMAC64.fullmatch(digest) is None:
            raise VendorTestFileError("allowlist HMAC is invalid")
        normalized = (version, digest)
        if normalized in seen:
            raise VendorTestFileError("allowlist entries must be unique")
        seen.add(normalized)
    return len(seen)


def _parse_dotenv(raw: str) -> tuple[list[str], dict[str, int]]:
    lines = raw.splitlines()
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_LINE.fullmatch(line)
        if match is None:
            raise VendorTestFileError("dotenv contains non-strict syntax")
        key = match.group(1)
        if key.upper() in _VENDOR_CREDENTIAL_KEYS:
            raise VendorTestFileError("dotenv must not contain vendor credentials")
        if key in positions:
            raise VendorTestFileError("dotenv contains duplicate keys")
        positions[key] = index
    return lines, positions


def require_pure_mock_dotenv(path: Path, *, expected_uid: int = 0) -> None:
    """marker 缺失时只接受仍处于严格纯 Mock 的首次激活前配置。"""

    _validate_private_file(path, expected_uid=expected_uid)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VendorTestFileError("dotenv is unavailable") from exc
    lines, positions = _parse_dotenv(raw)
    selected = {
        key: lines[positions[key]].split("=", 1)[1] if key in positions else None
        for key in PURE_MOCK_DOTENV_VALUES
    }
    if selected != dict(PURE_MOCK_DOTENV_VALUES):
        raise VendorTestFileError("dotenv must remain in pure Mock mode")


def reconcile_pure_mock_dotenv(path: Path, *, expected_uid: int = 0) -> bool:
    """仅在语义已是纯 Mock 时清理旧 Provider 键与行尾注释。"""

    _validate_private_file(path, expected_uid=expected_uid)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VendorTestFileError("dotenv is unavailable") from exc

    normalized_lines: list[str] = []
    values: dict[str, str] = {}
    seen_keys: set[str] = set()
    changed = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            normalized_lines.append(line)
            continue

        key_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if key_match is None:
            raise VendorTestFileError("dotenv contains non-reconcilable syntax")
        key = key_match.group(1)
        if key.upper() in _VENDOR_CREDENTIAL_KEYS:
            raise VendorTestFileError("dotenv must not contain vendor credentials")
        if key in seen_keys:
            raise VendorTestFileError("dotenv contains duplicate keys")
        seen_keys.add(key)
        if key in RETIRED_PROVIDER_DOTENV_KEYS:
            changed = True
            continue

        match = _DOTENV_LINE.fullmatch(line)
        if match is None:
            match = _DOTENV_INLINE_COMMENT_LINE.fullmatch(line)
            if match is None:
                raise VendorTestFileError("dotenv contains non-reconcilable syntax")
            normalized = f"{key}={match.group(2)}"
            changed = True
        else:
            normalized = line
        values[key] = match.group(2)
        normalized_lines.append(normalized)

    selected = {key: values.get(key) for key in PURE_MOCK_DOTENV_VALUES}
    if selected != dict(PURE_MOCK_DOTENV_VALUES):
        raise VendorTestFileError("dotenv must remain in pure Mock mode")
    payload = ("\n".join(normalized_lines) + "\n").encode()
    if payload != raw.encode():
        changed = True
    if changed:
        _atomic_replace(path, payload, expected_uid=expected_uid)
    require_pure_mock_dotenv(path, expected_uid=expected_uid)
    return changed


def activate_vendor_live_dotenv(path: Path, *, expected_uid: int = 0) -> None:
    """仅定向替换固定 activation key，保留其余严格配置。"""

    _validate_private_file(path, expected_uid=expected_uid)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VendorTestFileError("dotenv is unavailable") from exc
    lines, positions = _parse_dotenv(raw)
    for key, value in LIVE_DOTENV_VALUES.items():
        rendered = f"{key}={value}"
        if key in positions:
            lines[positions[key]] = rendered
        else:
            positions[key] = len(lines)
            lines.append(rendered)
    _atomic_replace(
        path,
        ("\n".join(lines) + "\n").encode(),
        expected_uid=expected_uid,
    )


def require_pii_free_evidence(value: object) -> None:
    """递归拒绝 evidence 中的手机号、HMAC、正文和凭据派生字段。"""

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str) or _FORBIDDEN_EVIDENCE_KEY.search(key):
                    raise VendorTestFileError("evidence contains forbidden fields")
                visit(nested)
            return
        if isinstance(item, list | tuple):
            for nested in item:
                visit(nested)
            return
        if isinstance(item, str) and (
            _PHONE.search(item) is not None or _HEX64.fullmatch(item) is not None
        ):
            raise VendorTestFileError("evidence contains forbidden values")
        if item is not None and not isinstance(item, str | int | float | bool):
            raise VendorTestFileError("evidence contains unsupported values")

    visit(value)


def write_pii_free_evidence(
    path: Path,
    value: Mapping[str, object],
    *,
    expected_uid: int = 0,
) -> None:
    require_pii_free_evidence(value)
    _atomic_replace(
        path,
        (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        expected_uid=expected_uid,
    )
