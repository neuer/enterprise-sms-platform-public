"""部署提供的离线常见/泄露密码库；提交密码只在内存中比较。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.settings import Settings

MAX_CORPUS_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 100000


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate corpus field")
        result[key] = value
    return result


class PasswordScreeningUnavailable(RuntimeError):
    """密码安全检查不可用；禁止创建或变更本地密码。"""


@dataclass(frozen=True, slots=True)
class OfflinePasswordScreen:
    """进程内不可变快照；更新需重启，过期后禁止继续使用。"""

    required: bool
    hashes: frozenset[str] = field(default_factory=frozenset, repr=False)
    expires_at: datetime | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> OfflinePasswordScreen:
        path = settings.auth_password_corpus_file
        required = settings.environment == "production" or path is not None
        if path is None:
            return cls(required)
        return cls.from_file(path)

    @classmethod
    def from_file(cls, path: Path) -> OfflinePasswordScreen:
        """只接受有限、带版本及到期时间的文件；错误不回显路径或内容。"""
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("invalid corpus")
            with path.open("rb") as stream:
                raw = stream.read(MAX_CORPUS_BYTES + 1)
            if len(raw) > MAX_CORPUS_BYTES:
                raise ValueError("oversized corpus")
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if set(data) != {"version", "source_ref", "expires_at", "sha256"}:
                raise ValueError("invalid corpus schema")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("invalid corpus version")
            if not isinstance(data["source_ref"], str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                data["source_ref"],
            ):
                raise ValueError("invalid corpus source")
            expiry = datetime.fromisoformat(data["expires_at"])
            hashes = data["sha256"]
            if expiry.tzinfo is None or not isinstance(hashes, list):
                raise ValueError("invalid corpus")
            if (
                not 1 <= len(hashes) <= MAX_ENTRIES
                or any(
                    not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                    for item in hashes
                )
                or len(set(hashes)) != len(hashes)
            ):
                raise ValueError("invalid corpus entries")
            return cls(True, frozenset(hashes), expiry)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return cls(True)

    def contains(self, password: str) -> bool:
        """密码原文和候选摘要不得写入日志、Redis、数据库或网络。"""
        if self.required and (
            not self.hashes or self.expires_at is None or self.expires_at <= datetime.now(UTC)
        ):
            raise PasswordScreeningUnavailable("密码安全检查暂不可用，请联系管理员")
        return hashlib.sha256(password.encode("utf-8")).hexdigest() in self.hashes
