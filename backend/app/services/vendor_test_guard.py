"""真实厂商联调号码白名单的严格读取、校验与原子写入。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from app.services.crypto import MAX_KEY_VERSION, CryptoService
from app.services.vendor_control_state import VendorControlStateGuard

MAX_ALLOWLIST_BYTES = 1_048_576
_DOCUMENT_FIELDS = frozenset({"schema_version", "entries"})
_ENTRY_FIELDS = frozenset({"key_version", "phone_hmac"})
_HMAC_RE = re.compile(r"[0-9a-f]{64}")


class VendorTestAllowlistError(ValueError):
    """真实联调白名单缺失、损坏或不符合严格合同。"""


class VendorTestRecipientDenied(PermissionError):
    """真实联调请求包含未登记号码，异常不携带号码或 HMAC。"""

    def __init__(self, denied_count: int) -> None:
        self.denied_count = denied_count
        super().__init__(f"vendor live test recipient denied: count={denied_count}")


@dataclass(frozen=True, slots=True, order=True)
class VendorTestRecipient:
    """白名单唯一允许持久化的 HMAC 索引条目。"""

    key_version: int
    phone_hmac: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VendorTestAllowlistError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    context: str,
) -> None:
    if set(value) != expected:
        raise VendorTestAllowlistError(f"{context} fields are invalid")


def _read_raw(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_ALLOWLIST_BYTES:
            raise VendorTestAllowlistError("vendor live test allowlist is too large")
        return path.read_text(encoding="utf-8")
    except VendorTestAllowlistError:
        raise
    except (OSError, UnicodeError) as exc:
        raise VendorTestAllowlistError("vendor live test allowlist is unavailable") from exc


def read_vendor_test_recipients(path: Path) -> frozenset[VendorTestRecipient]:
    """严格解析白名单，拒绝重复键、未知字段和非规范值。"""

    try:
        decoded = json.loads(_read_raw(path), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise VendorTestAllowlistError("vendor live test allowlist contains invalid JSON") from exc
    if type(decoded) is not dict:
        raise VendorTestAllowlistError("vendor live test allowlist must be an object")
    document = cast(dict[str, Any], decoded)
    _require_exact_fields(document, _DOCUMENT_FIELDS, "allowlist")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise VendorTestAllowlistError("allowlist schema_version is invalid")
    if type(document["entries"]) is not list:
        raise VendorTestAllowlistError("allowlist entries must be a list")

    entries: set[VendorTestRecipient] = set()
    for raw_entry in cast(list[object], document["entries"]):
        if type(raw_entry) is not dict:
            raise VendorTestAllowlistError("allowlist entry must be an object")
        entry = cast(dict[str, Any], raw_entry)
        _require_exact_fields(entry, _ENTRY_FIELDS, "allowlist entry")
        version = entry["key_version"]
        digest = entry["phone_hmac"]
        if type(version) is not int or not 1 <= version <= MAX_KEY_VERSION:
            raise VendorTestAllowlistError("allowlist key_version is invalid")
        if type(digest) is not str or _HMAC_RE.fullmatch(digest) is None:
            raise VendorTestAllowlistError("allowlist phone_hmac is invalid")
        recipient = VendorTestRecipient(version, digest)
        if recipient in entries:
            raise VendorTestAllowlistError("allowlist entries must not contain duplicates")
        entries.add(recipient)
    return frozenset(entries)


def write_vendor_test_recipients(
    path: Path,
    entries: Sequence[VendorTestRecipient],
    *,
    file_mode: int = 0o600,
    group_id: int | None = None,
) -> None:
    """以同目录原子替换写入白名单，并在可选后端组内只读共享。"""

    normalized = sorted(set(entries))
    if len(normalized) != len(entries):
        raise VendorTestAllowlistError("allowlist entries must not contain duplicates")
    if (group_id is None and file_mode != 0o600) or (
        group_id is not None and (group_id < 0 or file_mode != 0o640)
    ):
        raise VendorTestAllowlistError("allowlist file ownership policy is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": 1,
        "entries": [
            {"key_version": entry.key_version, "phone_hmac": entry.phone_hmac}
            for entry in normalized
        ],
    }
    payload_bytes = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            if group_id is not None:
                os.fchown(stream.fileno(), -1, group_id)
            os.fchmod(stream.fileno(), file_mode)
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class VendorTestRecipientGuard:
    """每次下发边界都重读白名单，使原子替换即时生效。"""

    def __init__(self, path: Path, crypto: CryptoService) -> None:
        self._path = path
        self._crypto = crypto

    @classmethod
    def load(cls, path: Path, crypto: CryptoService) -> VendorTestRecipientGuard:
        """启动时先严格验证文件，再返回动态重读检查器。"""

        read_vendor_test_recipients(path)
        return cls(path, crypto)

    def require_allowed(self, phones: Sequence[str]) -> None:
        """拒绝任一未登记号码，错误只暴露拒绝数量。"""

        entries = read_vendor_test_recipients(self._path)
        denied_count = 0
        for phone in phones:
            candidates = self._crypto.hmac_candidates(phone)
            if not any(
                VendorTestRecipient(version, digest) in entries
                for version, digest in candidates.items()
            ):
                denied_count += 1
        if denied_count:
            raise VendorTestRecipientDenied(denied_count)


class ControlStateGuard(Protocol):
    def require_fresh(self) -> object: ...


class RecipientGuard(Protocol):
    def require_allowed(self, phones: Sequence[str]) -> None: ...


class VendorLiveTestGuard:
    """先验证 root 控制状态，再读取或计算任何号码白名单数据。"""

    def __init__(
        self,
        control_state: ControlStateGuard,
        recipients: RecipientGuard,
    ) -> None:
        self.control_state = control_state
        self.recipients = recipients

    @classmethod
    def load(
        cls,
        allowlist_path: Path,
        crypto: CryptoService,
    ) -> VendorLiveTestGuard:
        return cls(
            VendorControlStateGuard(),
            VendorTestRecipientGuard.load(allowlist_path, crypto),
        )

    def require_fresh(self) -> object:
        return self.control_state.require_fresh()

    def require_allowed(self, phones: Sequence[str]) -> None:
        self.recipients.require_allowed(phones)

    def require_send_allowed(self, phones: Sequence[str]) -> None:
        self.require_fresh()
        self.require_allowed(phones)
