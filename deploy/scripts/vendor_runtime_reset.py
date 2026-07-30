#!/usr/bin/env python3
"""在宿主 lifecycle lock 内撤销正式厂商 runtime secret 副本。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Protocol

from prepare_runtime_secrets import VENDOR_REVOCATION_TOMBSTONE
from test_update_backup import require_inherited_lifecycle_lock

VENDOR_READER_SERVICES = ("api", "worker-realtime", "worker-bulk")
_CONTAINER_PROBE = (
    "from pathlib import Path;"
    "p=(Path('/run/secrets/vendor_secret_name'),"
    "Path('/run/secrets/vendor_secret_key'));"
    "raise SystemExit(0 if all(x.read_bytes()=="
    f"{VENDOR_REVOCATION_TOMBSTONE!r} for x in p) else 1)"
)


class VendorRuntimeResetError(RuntimeError):
    """runtime 撤销失败；消息不携带子进程输出或 secret 元数据。"""


class RuntimeResetResult(NamedTuple):
    status: str


class RuntimeResetOperations(Protocol):
    def require_lifecycle_lock(self) -> None: ...

    def runtime_is_revoked(self) -> bool: ...

    def readers_are_revoked(self) -> bool: ...

    def only_current_generation(self) -> bool: ...

    def revoke_runtime(self) -> None: ...

    def validate_compose(self) -> None: ...

    def stop_readers(self) -> None: ...

    def remove_readers(self) -> None: ...

    def start_readers(self) -> None: ...

    def cleanup_stale(self) -> None: ...


class FixedCommandRunner:
    """执行代码定义的固定 argv，并完全丢弃输出。"""

    def succeeds(self, command: Sequence[str]) -> bool:
        try:
            result = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0


class HostRuntimeResetOperations:
    """把撤销协议绑定到固定 preprocessor 与 Compose 服务集合。"""

    def __init__(
        self,
        *,
        root: Path,
        runtime_root: Path,
        runner: FixedCommandRunner | None = None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.runner = runner or FixedCommandRunner()
        self.preprocessor = root / "deploy/scripts/prepare_runtime_secrets.py"
        self.source_dir = root / "deploy/secrets"
        self.compose = (
            "docker",
            "compose",
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "deploy/docker-compose.yml"),
        )

    def _require_success(self, command: Sequence[str]) -> None:
        if not self.runner.succeeds(command):
            raise VendorRuntimeResetError("runtime reset failed")

    def _preprocessor(self, command: str, *arguments: str) -> bool:
        return self.runner.succeeds(
            [
                sys.executable,
                str(self.preprocessor),
                command,
                "--runtime-root",
                str(self.runtime_root),
                *arguments,
            ]
        )

    def require_lifecycle_lock(self) -> None:
        require_inherited_lifecycle_lock(self.runtime_root)

    def runtime_is_revoked(self) -> bool:
        return self._preprocessor("verify-vendor-revoked")

    def readers_are_revoked(self) -> bool:
        return all(
            self.runner.succeeds(
                [
                    *self.compose,
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    _CONTAINER_PROBE,
                ]
            )
            for service in VENDOR_READER_SERVICES
        )

    def only_current_generation(self) -> bool:
        return self._preprocessor("verify-only-current")

    def revoke_runtime(self) -> None:
        self._require_success(
            [
                sys.executable,
                str(self.preprocessor),
                "revoke-vendor",
                "--source-dir",
                str(self.source_dir),
                "--runtime-root",
                str(self.runtime_root),
                "--mode",
                "development",
            ]
        )

    def validate_compose(self) -> None:
        self._require_success([*self.compose, "config", "--quiet"])

    def stop_readers(self) -> None:
        self._require_success([*self.compose, "stop", *VENDOR_READER_SERVICES])

    def remove_readers(self) -> None:
        self._require_success([*self.compose, "rm", "-sf", *VENDOR_READER_SERVICES])

    def start_readers(self) -> None:
        self._require_success(
            [
                *self.compose,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                *VENDOR_READER_SERVICES,
            ]
        )

    def cleanup_stale(self) -> None:
        self._require_success(
            [
                sys.executable,
                str(self.preprocessor),
                "cleanup",
                "--runtime-root",
                str(self.runtime_root),
                "--stale",
            ]
        )


class VendorRuntimeResetManager:
    """幂等撤销 runtime，任何部分失败都禁止回滚旧厂商凭据。"""

    def __init__(self, operations: RuntimeResetOperations) -> None:
        self.operations = operations

    def reset(self) -> RuntimeResetResult:
        try:
            self.operations.require_lifecycle_lock()
            runtime_revoked = self.operations.runtime_is_revoked()
            readers_revoked = (
                self.operations.readers_are_revoked() if runtime_revoked else False
            )
            only_current = (
                self.operations.only_current_generation() if runtime_revoked else False
            )
            if runtime_revoked and readers_revoked and only_current:
                return RuntimeResetResult("runtime_revoked")

            if not runtime_revoked:
                self.operations.revoke_runtime()
            self.operations.validate_compose()
            if not readers_revoked:
                self.operations.stop_readers()
                self.operations.remove_readers()
                self.operations.start_readers()
            if not self.operations.readers_are_revoked():
                raise VendorRuntimeResetError("runtime reset failed")
            self.operations.cleanup_stale()
            if not (
                self.operations.runtime_is_revoked()
                and self.operations.readers_are_revoked()
                and self.operations.only_current_generation()
            ):
                raise VendorRuntimeResetError("runtime reset failed")
            return RuntimeResetResult("runtime_revoked")
        except Exception:
            raise VendorRuntimeResetError("runtime reset failed") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Revoke vendor runtime credentials")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    return parser


def _safe_absolute(path: Path) -> bool:
    return path.is_absolute() and path != Path(path.anchor) and ".." not in path.parts


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if (
        os.geteuid() != 0
        or os.environ.get("SMS_SECRETS_MODE") != "development"
        or not _safe_absolute(args.root)
        or not _safe_absolute(args.runtime_root)
    ):
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    try:
        result = VendorRuntimeResetManager(
            HostRuntimeResetOperations(
                root=args.root,
                runtime_root=args.runtime_root,
            )
        ).reset()
    except VendorRuntimeResetError:
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    print(json.dumps({"status": result.status}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
