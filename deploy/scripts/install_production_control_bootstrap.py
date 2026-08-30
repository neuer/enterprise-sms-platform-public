#!/usr/bin/env python3
"""Adopt the first immutable production-control manager and launcher.

This program is not a trust root for itself.  Before use, its exact reviewed
Git blob must be installed out of band as root:root 0555 at the fixed bootstrap
path.  It installs only root control bytes and a root-owned snapshot; it never
invokes Docker, Compose, systemd, secret management, a registry, or a release.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

REPOSITORY = Path("/opt/sms-platform")
APPROVAL_ROOT = Path("/etc/sms-platform/production-control-approved")
BOOTSTRAP_EXECUTABLE = Path(
    "/usr/local/libexec/sms-platform/production-control-bootstrap"
)
MANAGER_DESTINATION = Path(
    "/usr/local/libexec/sms-platform/production-control-snapshot"
)
LAUNCHER_DESTINATION = Path("/usr/local/sbin/sms-compose")
LEGACY_LAUNCHER_TARGET = Path("/opt/sms-platform/deploy/sms-compose")
LIFECYCLE_LOCK = Path("/run/sms-platform/secrets.lifecycle.lock")
LEGACY_INTENTS = (
    Path("/etc/sms-platform/production-host-assets.intent.json"),
    Path("/etc/sms-platform/production-host-assets.upgrade.intent.json"),
)

BOOTSTRAP_SOURCE = "deploy/scripts/install_production_control_bootstrap.py"
MANAGER_SOURCE = "deploy/scripts/production_control_snapshot.py"
LAUNCHER_SOURCE = "deploy/production-sms-compose-launcher"
GIT = "/usr/bin/git"
PYTHON = "/usr/bin/python3"
OPERATOR_UID = 1000
OPERATOR_GID = 1000
REPOSITORY_UID = 0
REPOSITORY_GID = OPERATOR_GID
REPOSITORY_MODE = 0o2770
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 60

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_OBJECT_RE = re.compile(r"[0-9a-f]{40,64}")
_SAFE_SOURCE_RE = re.compile(r"[A-Za-z0-9._+@/-]+")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class ProductionControlBootstrapError(RuntimeError):
    """The one-time control adoption cannot be proven safe."""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessRunner:
    """Run one fixed, shell-free command with a bounded result."""

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=dict(environment),
                shell=False,
                timeout=TIMEOUT_SECONDS,
                pass_fds=tuple(pass_fds),
                user=user,
                group=group,
                extra_groups=(
                    None if extra_groups is None else tuple(extra_groups)
                ),
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(argv, 126, b"", b"")
        if (
            len(completed.stdout) > MAX_OUTPUT_BYTES
            or len(completed.stderr) > MAX_OUTPUT_BYTES
        ):
            return subprocess.CompletedProcess(argv, 125, b"", b"")
        return completed


@dataclass(frozen=True, slots=True)
class Asset:
    source: str
    git_mode: str
    destination: Path
    payload: bytes
    sha256: str


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _manager_environment(lock_fd: int) -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "SMS_LIFECYCLE_LOCKED": "1",
        "SMS_LIFECYCLE_LOCK_FD": str(lock_fd),
    }


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_directory(
    path: Path,
    *,
    uid: int,
    gid: int,
    exact_mode: int | None = None,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlBootstrapError("required directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or (
            stat.S_IMODE(metadata.st_mode) != exact_mode
            if exact_mode is not None
            else bool(stat.S_IMODE(metadata.st_mode) & 0o022)
        )
    ):
        raise ProductionControlBootstrapError("required directory is unsafe")


def _read_regular(path: Path, *, uid: int, gid: int, mode: int) -> bytes:
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise ProductionControlBootstrapError("required file is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_gid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            before_identity != opened_identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != uid
            or opened.st_gid != gid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != mode
            or not 1 <= opened.st_size <= MAX_ASSET_BYTES
        ):
            raise ProductionControlBootstrapError("required file metadata is unsafe")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ProductionControlBootstrapError("required file is incomplete")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProductionControlBootstrapError("required file changed while reading")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != before_identity:
            raise ProductionControlBootstrapError("required file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage(path: Path, payload: bytes, *, uid: int, gid: int) -> Path:
    temporary = path.parent / f".{path.name}-{secrets.token_hex(8)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o555)
        os.fsync(descriptor)
        return temporary
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        raise ProductionControlBootstrapError("control asset staging failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class ProductionControlBootstrapInstaller:
    """Launcher-last, idempotent adoption of the first root control snapshot."""

    def __init__(
        self,
        *,
        repository: Path = REPOSITORY,
        approval_root: Path = APPROVAL_ROOT,
        bootstrap_executable: Path = BOOTSTRAP_EXECUTABLE,
        loaded_from: Path | None = None,
        manager_destination: Path = MANAGER_DESTINATION,
        launcher_destination: Path = LAUNCHER_DESTINATION,
        legacy_launcher_target: Path = LEGACY_LAUNCHER_TARGET,
        lifecycle_lock: Path = LIFECYCLE_LOCK,
        legacy_intents: Sequence[Path] = LEGACY_INTENTS,
        runner: CommandRunner | None = None,
        uid: int = 0,
        gid: int = 0,
        effective_uid: int | None = None,
        operator_uid: int = OPERATOR_UID,
        operator_gid: int = OPERATOR_GID,
        repository_uid: int = REPOSITORY_UID,
        repository_gid: int = REPOSITORY_GID,
        repository_mode: int = REPOSITORY_MODE,
        repository_metadata: os.stat_result | None = None,
        confirmed: bool = False,
    ) -> None:
        self.repository = repository
        self.approval_root = approval_root
        self.bootstrap_executable = bootstrap_executable
        self.loaded_from = Path(__file__) if loaded_from is None else loaded_from
        self.manager_destination = manager_destination
        self.launcher_destination = launcher_destination
        self.legacy_launcher_target = legacy_launcher_target
        self.lifecycle_lock = lifecycle_lock
        self.legacy_intents = tuple(legacy_intents)
        self.runner = runner or SubprocessRunner()
        self.uid = uid
        self.gid = gid
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self.operator_uid = operator_uid
        self.operator_gid = operator_gid
        self.repository_uid = repository_uid
        self.repository_gid = repository_gid
        self.repository_mode = repository_mode
        self.repository_metadata = repository_metadata
        self.confirmed = confirmed
        for path in (
            repository,
            approval_root,
            bootstrap_executable,
            manager_destination,
            launcher_destination,
            legacy_launcher_target,
            lifecycle_lock,
            *self.legacy_intents,
        ):
            if not path.is_absolute() or ".." in path.parts:
                raise ProductionControlBootstrapError("bootstrap path is invalid")

    def _approval(self, expected_commit: str) -> None:
        """Verify the independently installed OS approval marker without mutation."""

        _validate_directory(
            self.approval_root,
            uid=self.uid,
            gid=self.gid,
            exact_mode=0o755,
        )
        try:
            payload = _read_regular(
                self.approval_root / expected_commit,
                uid=self.uid,
                gid=self.gid,
                mode=0o444,
            )
        except ProductionControlBootstrapError as exc:
            raise ProductionControlBootstrapError(
                "production control approval marker is unsafe or unavailable"
            ) from exc
        if payload != f"{expected_commit}\n".encode("ascii"):
            raise ProductionControlBootstrapError(
                "production control approval marker content is invalid"
            )

    def _validate_repository(self) -> None:
        try:
            metadata = (
                self.repository.lstat()
                if self.repository_metadata is None
                else self.repository_metadata
            )
        except OSError as exc:
            raise ProductionControlBootstrapError(
                "production repository is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.repository_uid
            or metadata.st_gid != self.repository_gid
            or stat.S_IMODE(metadata.st_mode) != self.repository_mode
        ):
            raise ProductionControlBootstrapError(
                "production repository metadata is unsafe"
            )

    def _git_argv(self, *arguments: str) -> tuple[str, ...]:
        return (
            GIT,
            "--no-replace-objects",
            "-c",
            f"safe.directory={self.repository}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(self.repository),
            *arguments,
        )

    def _git(self, *arguments: str) -> bytes:
        completed = self.runner.run(
            self._git_argv(*arguments),
            environment=_git_environment(),
            user=self.operator_uid,
            group=self.operator_gid,
            extra_groups=(),
        )
        if completed.returncode != 0 or type(completed.stdout) is not bytes:
            raise ProductionControlBootstrapError("fixed Git read failed")
        return completed.stdout

    def _git_blob(self, commit: str, source: str, git_mode: str) -> bytes:
        pure = PurePosixPath(source)
        if (
            not source
            or _SAFE_SOURCE_RE.fullmatch(source) is None
            or pure.is_absolute()
            or str(pure) != source
            or any(part in {"", ".", "..", ".git"} for part in pure.parts)
        ):
            raise ProductionControlBootstrapError("Git source path is invalid")
        listing = self._git("ls-tree", "-z", commit, "--", source)
        fields, separator, observed = listing.partition(b"\t")
        metadata = fields.split()
        if (
            separator != b"\t"
            or observed != source.encode("ascii") + b"\0"
            or len(metadata) != 3
            or metadata[0] != git_mode.encode("ascii")
            or metadata[1] != b"blob"
            or _OBJECT_RE.fullmatch(metadata[2].decode("ascii", errors="ignore"))
            is None
        ):
            raise ProductionControlBootstrapError("required Git asset is invalid")
        object_id = metadata[2].decode("ascii")
        size_raw = self._git("cat-file", "-s", object_id)
        if (
            not size_raw.endswith(b"\n")
            or not size_raw[:-1].isdigit()
            or not 1 <= int(size_raw[:-1]) <= MAX_ASSET_BYTES
        ):
            raise ProductionControlBootstrapError("required Git asset size is invalid")
        payload = self._git("cat-file", "blob", object_id)
        if len(payload) != int(size_raw[:-1]):
            raise ProductionControlBootstrapError("required Git asset size changed")
        return payload

    def _assets(self, expected_commit: str) -> tuple[Asset, Asset]:
        if _COMMIT_RE.fullmatch(expected_commit) is None:
            raise ProductionControlBootstrapError(
                "expected commit must be 40 lowercase hexadecimal characters"
            )
        self._approval(expected_commit)
        if self.effective_uid != self.uid:
            raise ProductionControlBootstrapError("bootstrap requires root")
        if self.loaded_from != self.bootstrap_executable:
            raise ProductionControlBootstrapError(
                "bootstrap must be adopted to its fixed root-owned path"
            )
        self._validate_repository()
        if self._git("cat-file", "-t", expected_commit) != b"commit\n":
            raise ProductionControlBootstrapError("expected commit is unavailable")
        bootstrap = self._git_blob(expected_commit, BOOTSTRAP_SOURCE, "100644")
        installed_bootstrap = _read_regular(
            self.bootstrap_executable, uid=self.uid, gid=self.gid, mode=0o555
        )
        if installed_bootstrap != bootstrap:
            raise ProductionControlBootstrapError(
                "root-owned bootstrap does not match the expected Git blob"
            )
        manager_payload = self._git_blob(
            expected_commit, MANAGER_SOURCE, "100644"
        )
        launcher_payload = self._git_blob(
            expected_commit, LAUNCHER_SOURCE, "100755"
        )
        return (
            Asset(
                MANAGER_SOURCE,
                "100644",
                self.manager_destination,
                manager_payload,
                hashlib.sha256(manager_payload).hexdigest(),
            ),
            Asset(
                LAUNCHER_SOURCE,
                "100755",
                self.launcher_destination,
                launcher_payload,
                hashlib.sha256(launcher_payload).hexdigest(),
            ),
        )

    def _matches(self, asset: Asset) -> bool:
        try:
            payload = _read_regular(
                asset.destination, uid=self.uid, gid=self.gid, mode=0o555
            )
        except ProductionControlBootstrapError:
            return False
        return hashlib.sha256(payload).hexdigest() == asset.sha256

    def _legacy_launcher(self) -> bool:
        try:
            metadata = self.launcher_destination.lstat()
            target = os.readlink(self.launcher_destination)
        except OSError:
            return False
        return (
            stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == self.uid
            and metadata.st_gid == self.gid
            and target == str(self.legacy_launcher_target)
        )

    def _preflight(self, manager: Asset, launcher: Asset) -> str:
        for intent in self.legacy_intents:
            if _lexists(intent):
                raise ProductionControlBootstrapError(
                    "legacy host-asset transaction is incomplete"
                )
        for parent in {
            self.bootstrap_executable.parent,
            self.manager_destination.parent,
            self.launcher_destination.parent,
        }:
            _validate_directory(parent, uid=self.uid, gid=self.gid)
        manager_state = "installed" if self._matches(manager) else "absent"
        if manager_state == "absent" and _lexists(self.manager_destination):
            raise ProductionControlBootstrapError(
                "snapshot manager destination is not adoptable"
            )
        if self._matches(launcher):
            launcher_state = "installed"
        elif self._legacy_launcher():
            launcher_state = "legacy"
        else:
            raise ProductionControlBootstrapError(
                "launcher is neither the exact legacy link nor the target asset"
            )
        if launcher_state == "installed" and manager_state != "installed":
            raise ProductionControlBootstrapError(
                "installed launcher has no verified snapshot manager"
            )
        return launcher_state

    @contextmanager
    def _lock(self) -> Any:
        descriptor = -1
        try:
            descriptor = os.open(
                self.lifecycle_lock,
                os.O_RDWR | _NOFOLLOW,
            )
            metadata = os.fstat(descriptor)
            expected = self.lifecycle_lock.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.uid
                or metadata.st_gid != self.gid
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (metadata.st_dev, metadata.st_ino)
                != (expected.st_dev, expected.st_ino)
            ):
                raise ProductionControlBootstrapError(
                    "lifecycle lock metadata is unsafe"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ProductionControlBootstrapError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ProductionControlBootstrapError(
                "lifecycle lock is unavailable"
            ) from exc
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _install_manager(self, manager: Asset) -> None:
        if self._matches(manager):
            return
        if _lexists(manager.destination):
            raise ProductionControlBootstrapError(
                "snapshot manager destination changed"
            )
        temporary = _stage(
            manager.destination, manager.payload, uid=self.uid, gid=self.gid
        )
        try:
            if _lexists(manager.destination):
                raise ProductionControlBootstrapError(
                    "snapshot manager destination changed"
                )
            os.replace(temporary, manager.destination)
            _fsync_directory(manager.destination.parent)
        except OSError as exc:
            raise ProductionControlBootstrapError(
                "snapshot manager publication failed"
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        if not self._matches(manager):
            raise ProductionControlBootstrapError(
                "snapshot manager verification failed"
            )

    def _manager(
        self,
        command: str,
        expected_commit: str,
        *,
        lock_fd: int,
    ) -> dict[str, Any]:
        argv = [PYTHON, "-I", str(self.manager_destination), command]
        if command != "status":
            argv.extend(("--expected-commit", expected_commit))
        completed = self.runner.run(
            argv,
            environment=_manager_environment(lock_fd),
            pass_fds=(lock_fd,),
        )
        if completed.returncode != 0 or completed.stderr:
            raise ProductionControlBootstrapError(
                f"snapshot manager {command} failed"
            )
        try:
            value = json.loads(completed.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionControlBootstrapError(
                f"snapshot manager {command} returned invalid status"
            ) from exc
        if type(value) is not dict or value.get("commit") != expected_commit:
            raise ProductionControlBootstrapError(
                f"snapshot manager {command} returned invalid status"
            )
        return cast(dict[str, Any], value)

    def _manager_status(self, expected_commit: str, *, lock_fd: int) -> None:
        result = self._manager("status", expected_commit, lock_fd=lock_fd)
        if (
            result.get("status") != "active"
            or result.get("current_target") != f"versions/{expected_commit}"
        ):
            raise ProductionControlBootstrapError(
                "snapshot manager is not active at the expected commit"
            )

    def _install_launcher(self, launcher: Asset) -> None:
        if self._matches(launcher):
            return
        if not self._legacy_launcher():
            raise ProductionControlBootstrapError("legacy launcher changed")
        temporary = _stage(
            launcher.destination, launcher.payload, uid=self.uid, gid=self.gid
        )
        try:
            if not self._legacy_launcher():
                raise ProductionControlBootstrapError("legacy launcher changed")
            os.replace(temporary, launcher.destination)
            _fsync_directory(launcher.destination.parent)
        except OSError as exc:
            raise ProductionControlBootstrapError(
                "stable launcher publication failed"
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink()
        if not self._matches(launcher):
            raise ProductionControlBootstrapError("stable launcher verification failed")

    def plan(self, expected_commit: str) -> dict[str, object]:
        manager, launcher = self._assets(expected_commit)
        with self._lock():
            launcher_state = self._preflight(manager, launcher)
        return {
            "action": "plan",
            "commit": expected_commit,
            "launcher": launcher_state,
            "manager": "installed" if self._matches(manager) else "absent",
            "status": "ready",
        }

    def apply(self, expected_commit: str) -> dict[str, object]:
        if not self.confirmed:
            raise ProductionControlBootstrapError(
                "explicit reviewed bootstrap confirmation is required"
            )
        manager, launcher = self._assets(expected_commit)
        with self._lock() as lock_fd:
            self._approval(expected_commit)
            self._preflight(manager, launcher)
            self._install_manager(manager)
            prepared = self._manager(
                "prepare", expected_commit, lock_fd=lock_fd
            )
            if prepared.get("status") != "prepared":
                raise ProductionControlBootstrapError(
                    "snapshot manager did not prepare the expected commit"
                )
            activated = self._manager(
                "activate", expected_commit, lock_fd=lock_fd
            )
            if activated.get("status") != "active":
                raise ProductionControlBootstrapError(
                    "snapshot manager did not activate the expected commit"
                )
            self._manager_status(expected_commit, lock_fd=lock_fd)
            self._install_launcher(launcher)
        return {
            "action": "apply",
            "commit": expected_commit,
            "status": "installed",
        }

    def status(self, expected_commit: str) -> dict[str, object]:
        manager, launcher = self._assets(expected_commit)
        with self._lock() as lock_fd:
            self._approval(expected_commit)
            self._preflight(manager, launcher)
            if not self._matches(manager) or not self._matches(launcher):
                raise ProductionControlBootstrapError(
                    "production control assets are not fully installed"
                )
            self._manager_status(expected_commit, lock_fd=lock_fd)
        return {
            "action": "status",
            "commit": expected_commit,
            "status": "installed",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply", "status"):
        command = subparsers.add_parser(action)
        command.add_argument("--expected-commit", required=True)
        if action == "apply":
            command.add_argument(
                "--confirm-reviewed-control-bootstrap", action="store_true"
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    installer = ProductionControlBootstrapInstaller(
        confirmed=getattr(arguments, "confirm_reviewed_control_bootstrap", False)
    )
    try:
        result = getattr(installer, arguments.action)(arguments.expected_commit)
    except ProductionControlBootstrapError:
        print(
            json.dumps(
                {"action": arguments.action, "status": "blocked"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
