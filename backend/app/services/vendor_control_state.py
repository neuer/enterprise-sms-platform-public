"""后端对 root agent 安全状态投影的严格读取与新鲜度门禁。"""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

CONTROL_STATE_FILE = Path("/run/vendor-test/control-state.json")
CONTROL_STATE_UID = 0
CONTROL_STATE_GID = 10001
MAX_CONTROL_STATE_BYTES = 4096
MAX_HEARTBEAT_AGE = timedelta(seconds=30)
MAX_FUTURE_SKEW = timedelta(seconds=5)
_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "heartbeat_at",
        "credential_configured",
        "active_recipient_count",
        "pause_kind",
        "daily_limit",
    }
)
_MODES = frozenset({"setup_required", "inactive", "controlled", "blocked"})
_PAUSE_KINDS = frozenset({"manual", "critical", "daily"})


class VendorControlStateUnavailable(RuntimeError):
    """控制状态缺失、陈旧或尚未允许下发；异常不携带文件内容。"""

    def __init__(self, message: str, *, requires_critical_pause: bool) -> None:
        self.requires_critical_pause = requires_critical_pause
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VendorControlState:
    mode: str
    heartbeat_at: datetime
    credential_configured: bool
    active_recipient_count: int
    pause_kind: str | None
    daily_limit: int


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            )
        document[key] = value
    return document


class VendorControlStateGuard:
    """每次调用重新读取固定 0640 文件，超过 30 秒立即 fail-closed。"""

    def __init__(
        self,
        path: Path = CONTROL_STATE_FILE,
        *,
        expected_uid: int = CONTROL_STATE_UID,
        expected_gid: int = CONTROL_STATE_GID,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.clock = clock

    def _read(self) -> VendorControlState:
        try:
            metadata = self.path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != self.expected_gid
                or stat.S_IMODE(metadata.st_mode) != 0o640
                or metadata.st_size < 1
                or metadata.st_size > MAX_CONTROL_STATE_BYTES
            ):
                raise OSError("unsafe state projection")
            document = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except VendorControlStateUnavailable:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise VendorControlStateUnavailable(
                "真实联调控制状态不可用",
                requires_critical_pause=True,
            ) from None
        if type(document) is not dict or set(document) != _FIELDS:
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            )
        typed = cast(dict[str, object], document)
        if typed.get("schema_version") != 1:
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            )
        mode = typed.get("mode")
        heartbeat_at = typed.get("heartbeat_at")
        configured = typed.get("credential_configured")
        count = typed.get("active_recipient_count")
        pause_kind = typed.get("pause_kind")
        daily_limit = typed.get("daily_limit")
        if (
            mode not in _MODES
            or type(heartbeat_at) is not str
            or type(configured) is not bool
            or type(count) is not int
            or count < 0
            or (pause_kind is not None and pause_kind not in _PAUSE_KINDS)
            or type(daily_limit) is not int
            or daily_limit != 100
        ):
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            )
        try:
            parsed_heartbeat = datetime.fromisoformat(heartbeat_at)
        except ValueError:
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            ) from None
        if parsed_heartbeat.tzinfo is None:
            raise VendorControlStateUnavailable(
                "真实联调控制状态无效",
                requires_critical_pause=True,
            )
        return VendorControlState(
            mode=str(mode),
            heartbeat_at=parsed_heartbeat.astimezone(UTC),
            credential_configured=configured,
            active_recipient_count=count,
            pause_kind=str(pause_kind) if pause_kind is not None else None,
            daily_limit=daily_limit,
        )

    def read_fresh(self) -> VendorControlState:
        state = self.read()
        now = self.clock().astimezone(UTC)
        age = now - state.heartbeat_at
        if age > MAX_HEARTBEAT_AGE or age < -MAX_FUTURE_SKEW:
            raise VendorControlStateUnavailable(
                "真实联调控制状态已过期",
                requires_critical_pause=True,
            )
        return state

    def require_fresh(self) -> VendorControlState:
        state = self.read_fresh()
        if (
            state.mode != "controlled"
            or not state.credential_configured
            or state.active_recipient_count < 1
            or state.pause_kind is not None
        ):
            raise VendorControlStateUnavailable(
                "真实联调尚未就绪",
                requires_critical_pause=False,
            )
        return state

    def read(self) -> VendorControlState:
        """返回经过权限与严格 schema 校验的安全投影，不要求已激活。"""

        return self._read()
