#!/usr/bin/env python3
"""在开发测试环境中重启控制代理并验证新的安全投影。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

CONTROL_STATE = Path("/var/lib/sms-platform/vendor-test/control-state.json")
CONTROL_SOCKET = Path("/run/sms-platform/vendor-control/vendor-control.sock")
CONTROL_UNIT = "vendor-control-agent.service"
STATE_FIELDS = {
    "schema_version",
    "mode",
    "heartbeat_at",
    "credential_configured",
    "active_recipient_count",
    "pause_kind",
    "daily_limit",
}


class CommandRunner(Protocol):
    """固定系统命令执行边界。"""

    def run(self, *argv: str) -> None: ...


class SystemCommandRunner:
    """只允许本模块声明的固定 systemctl 调用。"""

    def run(self, *argv: str) -> None:
        subprocess.run(
            argv,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class VendorControlAgentReloader:
    """重启代理后验证 unit、socket 和无敏感状态投影。"""

    def __init__(
        self,
        *,
        state_path: Path,
        socket_path: Path,
        expected_uid: int,
        expected_gid: int,
        runner: CommandRunner,
        clock: Callable[[], datetime],
        ready_timeout_s: float = 30,
    ) -> None:
        self.state_path = state_path
        self.socket_path = socket_path
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.runner = runner
        self.clock = clock
        self.ready_timeout_s = ready_timeout_s

    def _socket_inode(self) -> int:
        metadata = self.socket_path.lstat()
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid)
            != (self.expected_uid, self.expected_gid)
            or stat.S_IMODE(metadata.st_mode) != 0o660
        ):
            raise RuntimeError("vendor control reload ready check failed")
        return metadata.st_ino

    def _validate_projection(self) -> None:
        metadata = self.state_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid)
            != (self.expected_uid, self.expected_gid)
            or stat.S_IMODE(metadata.st_mode) != 0o640
        ):
            raise RuntimeError("vendor control reload ready check failed")
        document = json.loads(self.state_path.read_text(encoding="utf-8"))
        if type(document) is not dict or set(document) != STATE_FIELDS:
            raise RuntimeError("vendor control reload ready check failed")
        try:
            heartbeat = datetime.fromisoformat(
                str(document["heartbeat_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("vendor control reload ready check failed") from exc
        age = (self.clock().astimezone(UTC) - heartbeat.astimezone(UTC)).total_seconds()
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["mode"]
            not in {"setup_required", "inactive", "controlled", "blocked"}
            or type(document["credential_configured"]) is not bool
            or type(document["active_recipient_count"]) is not int
            or document["active_recipient_count"] < 0
            or document["pause_kind"] not in {None, "manual", "critical", "daily"}
            or document["daily_limit"] != 100
            or heartbeat.tzinfo is None
            or age < -5
            or age > 30
        ):
            raise RuntimeError("vendor control reload ready check failed")

    def _ready(self, *, old_inode: int) -> bool:
        try:
            new_inode = self._socket_inode()
            self._validate_projection()
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            return False
        return new_inode != old_inode

    def run(self) -> dict[str, str]:
        old_inode = self._socket_inode()
        try:
            self.runner.run("systemctl", "restart", CONTROL_UNIT)
            self.runner.run("systemctl", "is-active", "--quiet", CONTROL_UNIT)
        except Exception as exc:
            raise RuntimeError("vendor control reload failed") from exc

        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            if self._ready(old_inode=old_inode):
                return {"status": "ready"}
            if time.monotonic() >= deadline:
                raise RuntimeError("vendor control reload ready check failed")
            time.sleep(0.1)


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("vendor control reload requires root")
    result = VendorControlAgentReloader(
        state_path=CONTROL_STATE,
        socket_path=CONTROL_SOCKET,
        expected_uid=0,
        expected_gid=10001,
        runner=SystemCommandRunner(),
        clock=lambda: datetime.now(UTC),
    ).run()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
