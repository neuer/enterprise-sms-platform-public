#!/usr/bin/env python3
"""规划、安装并只读核验固定的生产宿主资产。"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SOURCE_ROOT = Path("/opt/sms-platform")
ETC_ROOT = Path("/etc/sms-platform")
SYSTEMD_ROOT = Path("/etc/systemd/system")
LOCAL_SBIN_ROOT = Path("/usr/local/sbin")
DOCKER_ROOT = Path("/var/lib/docker")
RELEASES_ROOT = Path("/var/lib/sms-platform/releases")
STATE_PATH = ETC_ROOT / "production-host-assets.json"
INTENT_NAME = "production-host-assets.intent.json"
UPGRADE_INTENT_NAME = "production-host-assets.upgrade.intent.json"
LOCK_NAME = ".host-assets.lock"
INTENT_SCHEMA_VERSION = 1
UPGRADE_INTENT_SCHEMA_VERSION = 1
GIT_BINARY = "/usr/bin/git"
PYTHON_BINARY = "/usr/bin/python3"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
FINDMNT_BINARY = "/usr/bin/findmnt"
COMMAND_TIMEOUT_SECONDS = 30
UPGRADE_ACCEPTANCE_TIMEOUT_SECONDS = 60
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
COMMAND_OUTPUT_LIMIT_RETURN_CODE = 125
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_INLINE_PREFLIGHT_BYTES = 128 * 1024
MAX_STATE_BYTES = 128 * 1024
MAX_INTENT_BYTES = 128 * 1024
MAX_UID = 2**32 - 2
STATE_SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DECIMAL_UID_RE = re.compile(r"(?:0|[1-9][0-9]*)")
PREBOOTSTRAP_REPAIR_FROM_COMMIT = "555fb20b0d630ece9099a88a463eb1ce1121c012"
PREBOOTSTRAP_REPAIR_ASSETS = frozenset(
    {
        "storage-preflight",
        "storage-unit",
        "partition-service",
        "backup-service",
        "restore-drill-service",
        "lifecycle-status-service",
    }
)
HOST_MOUNTINFO_CREDENTIAL_LINE = b"LoadCredential=sms-host-mountinfo:/proc/1/mountinfo"
HOST_MOUNTINFO_MARKER_LINE = b"Environment=SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL=1"
HOST_MOUNTINFO_UNIT_ASSETS = frozenset(
    {
        "storage-unit",
        "partition-service",
        "backup-service",
        "restore-drill-service",
        "lifecycle-status-service",
    }
)

AssetKind = Literal["regular", "symlink"]


class HostAssetInstallError(RuntimeError):
    """生产宿主资产未满足失败关闭安装合同。"""


def _validated_git_sudo_uid(
    *,
    source_owner_uid: int,
    effective_uid: int,
    raw_sudo_uid: str | None,
) -> str | None:
    """仅为 root 读取匹配 sudo 操作者所有权的 checkout 保留 SUDO_UID。"""

    if source_owner_uid == effective_uid:
        return None
    if effective_uid != 0 or raw_sudo_uid is None:
        raise HostAssetInstallError("source checkout ownership is not trusted")
    if DECIMAL_UID_RE.fullmatch(raw_sudo_uid) is None:
        raise HostAssetInstallError("source checkout ownership is not trusted")
    parsed_uid = int(raw_sudo_uid, 10)
    if parsed_uid > MAX_UID or parsed_uid != source_owner_uid:
        raise HostAssetInstallError("source checkout ownership is not trusted")
    return str(parsed_uid)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded result from one read-only host command."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        git_sudo_uid: str | None = None,
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult: ...


def _is_fixed_read_only_git_command(argv: Sequence[str]) -> bool:
    if (
        len(argv) < 8
        or argv[0] != GIT_BINARY
        or argv[1] != "-C"
        or not Path(argv[2]).is_absolute()
        or tuple(argv[3:7])
        != (
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
        )
    ):
        return False
    arguments = tuple(argv[7:])
    return (
        arguments == ("rev-parse", "--verify", "HEAD^{commit}")
        or (
            len(arguments) == 3
            and arguments[:2] == ("cat-file", "-t")
            and COMMIT_RE.fullmatch(arguments[2]) is not None
        )
        or arguments == ("status", "--porcelain=v1", "--untracked-files=all")
        or (
            len(arguments) == 5
            and arguments[0:2] == ("ls-tree", "-z")
            and COMMIT_RE.fullmatch(arguments[2]) is not None
            and arguments[3] == "--"
        )
        or (
            len(arguments) == 3
            and arguments[:2] == ("cat-file", "blob")
            and COMMIT_RE.fullmatch(arguments[2].split(":", 1)[0]) is not None
            and ":" in arguments[2]
        )
    )


class SubprocessRunner:
    """Run fixed, non-shell commands with a minimal environment."""

    def run(
        self,
        argv: Sequence[str],
        *,
        git_sudo_uid: str | None = None,
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult:
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONNOUSERSITE": "1",
        }
        if git_sudo_uid is not None:
            if DECIMAL_UID_RE.fullmatch(
                git_sudo_uid
            ) is None or not _is_fixed_read_only_git_command(argv):
                return CommandResult(returncode=126)
            environment["SUDO_UID"] = git_sudo_uid
        if timeout_seconds <= 0 or timeout_seconds > UPGRADE_ACCEPTANCE_TIMEOUT_SECONDS:
            return CommandResult(returncode=126)
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                env=environment,
            )
        except FileNotFoundError:
            return CommandResult(returncode=127)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124)
        except OSError:
            return CommandResult(returncode=126)
        if (
            len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stdout) + len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            return CommandResult(returncode=COMMAND_OUTPUT_LIMIT_RETURN_CODE)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """One source-bound host asset and its fixed destination contract."""

    name: str
    source_relative: Path
    destination: Path
    mode: int
    git_mode: str
    kind: AssetKind = "regular"
    symlink_target: Path | None = None

    def __post_init__(self) -> None:
        if self.kind == "symlink" and self.symlink_target is None:
            raise ValueError("symlink asset requires a fixed target")
        if self.kind == "regular" and self.symlink_target is not None:
            raise ValueError("regular asset cannot have a symlink target")


@dataclass(frozen=True, slots=True)
class AssetSnapshot:
    """Bytes proven to match one path in the expected commit."""

    spec: AssetSpec
    payload: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class InstalledState:
    """A structurally valid state manifest, before destination verification."""

    payload: bytes
    commit: str
    assets_by_name: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class UpgradeChange:
    """One regular asset whose exact old bytes differ from the target snapshot."""

    snapshot: AssetSnapshot
    old_item: Mapping[str, object]


def build_asset_specs(
    *,
    source_root: Path,
    etc_root: Path,
    systemd_root: Path,
    local_sbin_root: Path,
) -> tuple[AssetSpec, ...]:
    """Return the exact 17 regular files and one fixed wrapper symlink."""

    regular = (
        (
            "compose-env",
            "deploy/systemd/compose.env.example",
            etc_root / "compose.env",
            0o600,
            "100644",
        ),
        (
            "storage-preflight",
            "deploy/scripts/storage_preflight.py",
            local_sbin_root / "sms-storage-preflight",
            0o755,
            "100755",
        ),
        (
            "storage-unit",
            "deploy/systemd/sms-storage-preflight.service",
            systemd_root / "sms-storage-preflight.service",
            0o644,
            "100644",
        ),
        (
            "docker-storage-dropin",
            "deploy/systemd/docker.service.d/10-sms-platform-storage.conf",
            systemd_root / "docker.service.d/10-sms-platform-storage.conf",
            0o644,
            "100644",
        ),
        (
            "platform-storage-dropin",
            "deploy/systemd/sms-platform.service.d/10-storage-preflight.conf",
            systemd_root / "sms-platform.service.d/10-storage-preflight.conf",
            0o644,
            "100644",
        ),
        (
            "platform-unit",
            "deploy/systemd/sms-platform.service",
            systemd_root / "sms-platform.service",
            0o644,
            "100644",
        ),
        (
            "vendor-control-unit",
            "deploy/systemd/vendor-control-agent.service",
            systemd_root / "vendor-control-agent.service",
            0o644,
            "100644",
        ),
        (
            "lifecycle-config",
            "deploy/lifecycle.server.example.json",
            etc_root / "lifecycle.json",
            0o600,
            "100644",
        ),
        (
            "lifecycle-env",
            "deploy/systemd/lifecycle.env.example",
            etc_root / "lifecycle.env",
            0o600,
            "100644",
        ),
        (
            "partition-service",
            "deploy/systemd/sms-partition-maintenance.service",
            systemd_root / "sms-partition-maintenance.service",
            0o644,
            "100644",
        ),
        (
            "partition-timer",
            "deploy/systemd/sms-partition-maintenance.timer",
            systemd_root / "sms-partition-maintenance.timer",
            0o644,
            "100644",
        ),
        (
            "backup-service",
            "deploy/systemd/sms-backup.service",
            systemd_root / "sms-backup.service",
            0o644,
            "100644",
        ),
        (
            "backup-timer",
            "deploy/systemd/sms-backup.timer",
            systemd_root / "sms-backup.timer",
            0o644,
            "100644",
        ),
        (
            "restore-drill-service",
            "deploy/systemd/sms-restore-drill.service",
            systemd_root / "sms-restore-drill.service",
            0o644,
            "100644",
        ),
        (
            "restore-drill-timer",
            "deploy/systemd/sms-restore-drill.timer",
            systemd_root / "sms-restore-drill.timer",
            0o644,
            "100644",
        ),
        (
            "lifecycle-status-service",
            "deploy/systemd/sms-lifecycle-status.service",
            systemd_root / "sms-lifecycle-status.service",
            0o644,
            "100644",
        ),
        (
            "lifecycle-status-timer",
            "deploy/systemd/sms-lifecycle-status.timer",
            systemd_root / "sms-lifecycle-status.timer",
            0o644,
            "100644",
        ),
    )
    specs = tuple(
        AssetSpec(
            name=name,
            source_relative=Path(source),
            destination=destination,
            mode=mode,
            git_mode=git_mode,
        )
        for name, source, destination, mode, git_mode in regular
    )
    wrapper = AssetSpec(
        name="compose-wrapper",
        source_relative=Path("deploy/sms-compose"),
        destination=local_sbin_root / "sms-compose",
        mode=0o755,
        git_mode="100755",
        kind="symlink",
        symlink_target=source_root / "deploy/sms-compose",
    )
    return (*specs, wrapper)


def _read_regular_no_follow(
    path: Path, maximum_bytes: int
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HostAssetInstallError("required regular file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise HostAssetInstallError("required regular file is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise HostAssetInstallError("required regular file exceeds its size limit")
        return payload, metadata
    finally:
        os.close(descriptor)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_existing_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    exact_mode: int | None = None,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostAssetInstallError("required host directory is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise HostAssetInstallError("required host directory is unsafe")


def _validate_empty_docker_root(
    path: Path,
    *,
    filesystem: str,
    expected_uid: int,
    expected_gid: int,
) -> None:
    """固定 Docker 挂载根只允许空目录或 ext4 固有的安全 lost+found。"""

    _assert_existing_path_chain_no_symlinks(path)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise HostAssetInstallError("Docker root is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o711
        ):
            raise HostAssetInstallError("Docker root is unsafe")
        observed_lost_found = False
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if (
                    filesystem != "ext4"
                    or observed_lost_found
                    or entry.name != "lost+found"
                ):
                    raise HostAssetInstallError("Docker root is not empty")
                entry_metadata = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(entry_metadata.st_mode)
                    or stat.S_ISLNK(entry_metadata.st_mode)
                    or entry_metadata.st_uid != expected_uid
                    or entry_metadata.st_gid != expected_gid
                    or stat.S_IMODE(entry_metadata.st_mode) != 0o700
                    or entry_metadata.st_dev != metadata.st_dev
                ):
                    raise HostAssetInstallError("Docker root lost+found is unsafe")
                lost_found_descriptor: int | None = None
                try:
                    lost_found_descriptor = os.open(
                        entry.name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                    with os.scandir(lost_found_descriptor) as recovered_entries:
                        if next(recovered_entries, None) is not None:
                            raise HostAssetInstallError(
                                "Docker root lost+found is not empty"
                            )
                except OSError as exc:
                    raise HostAssetInstallError(
                        "Docker root lost+found could not be inspected"
                    ) from exc
                finally:
                    if lost_found_descriptor is not None:
                        os.close(lost_found_descriptor)
                observed_lost_found = True
    except OSError as exc:
        raise HostAssetInstallError("Docker root could not be inspected") from exc
    finally:
        os.close(descriptor)


def _assert_existing_path_chain_no_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise HostAssetInstallError("host path chain is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HostAssetInstallError("host path chain contains a symlink")


def _create_fixed_directory(
    path: Path,
    *,
    mode: int,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if _lexists(path):
        _validate_existing_directory(
            path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            exact_mode=mode,
        )
        return
    _assert_existing_path_chain_no_symlinks(path.parent)
    try:
        os.mkdir(path, mode)
        os.chown(path, expected_uid, expected_gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as exc:
        raise HostAssetInstallError(
            "fixed host directory could not be created"
        ) from exc
    _validate_existing_directory(
        path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        exact_mode=mode,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_regular(
    destination: Path,
    payload: bytes,
    *,
    mode: int,
    expected_uid: int,
    expected_gid: int,
) -> Path:
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, expected_uid, expected_gid)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return temporary


def _commit_regular_no_replace(temporary: Path, destination: Path) -> None:
    """Publish a staged file without following or overwriting the destination."""

    try:
        os.link(
            temporary,
            destination,
            src_dir_fd=None,
            dst_dir_fd=None,
            follow_symlinks=False,
        )
        temporary.unlink()
        _fsync_directory(destination.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HostAssetInstallError(
            "host asset destination already exists or is unsafe"
        ) from exc


def _commit_regular_replace(temporary: Path, destination: Path) -> None:
    """Replace an installer-owned control file after a durable stage."""

    try:
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HostAssetInstallError(
            "installer control file could not be persisted"
        ) from exc


def _commit_symlink_no_replace(
    spec: AssetSpec, *, expected_uid: int, expected_gid: int
) -> None:
    assert spec.symlink_target is not None
    try:
        os.symlink(spec.symlink_target, spec.destination)
        os.chown(
            spec.destination,
            expected_uid,
            expected_gid,
            follow_symlinks=False,
        )
        _fsync_directory(spec.destination.parent)
    except OSError as exc:
        raise HostAssetInstallError(
            "host asset symlink could not be installed"
        ) from exc


def _validate_wrapper_target(spec: AssetSpec, expected_sha256: str) -> None:
    if spec.kind != "symlink" or spec.symlink_target is None:
        raise HostAssetInstallError("wrapper contract is invalid")
    _assert_existing_path_chain_no_symlinks(spec.symlink_target.parent)
    payload, metadata = _read_regular_no_follow(spec.symlink_target, MAX_ASSET_BYTES)
    if (
        stat.S_IMODE(metadata.st_mode) != spec.mode
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise HostAssetInstallError("wrapper target has drifted")


def _validate_installed_wrapper(
    spec: AssetSpec,
    expected_sha256: str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        metadata = spec.destination.lstat()
    except OSError as exc:
        raise HostAssetInstallError("installed wrapper is unavailable") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or os.readlink(spec.destination) != str(spec.symlink_target)
    ):
        raise HostAssetInstallError("installed wrapper has drifted")
    _validate_wrapper_target(spec, expected_sha256)


def _state_payload(expected_commit: str, snapshots: Sequence[AssetSnapshot]) -> bytes:
    assets: list[dict[str, object]] = []
    for snapshot in snapshots:
        item: dict[str, object] = {
            "destination": str(snapshot.spec.destination),
            "kind": snapshot.spec.kind,
            "mode": f"{snapshot.spec.mode:04o}",
            "name": snapshot.spec.name,
            "sha256": snapshot.sha256,
        }
        if snapshot.spec.symlink_target is not None:
            item["target"] = str(snapshot.spec.symlink_target)
        assets.append(item)
    return (
        json.dumps(
            {
                "assets": assets,
                "schema_version": STATE_SCHEMA_VERSION,
                "source_commit": expected_commit,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


class ProductionHostAssetInstaller:
    """Fail-closed controller for the one-time host asset installation."""

    def __init__(
        self,
        *,
        source_root: Path = SOURCE_ROOT,
        etc_root: Path = ETC_ROOT,
        systemd_root: Path = SYSTEMD_ROOT,
        local_sbin_root: Path = LOCAL_SBIN_ROOT,
        docker_root: Path = DOCKER_ROOT,
        releases_root: Path = RELEASES_ROOT,
        state_path: Path | None = None,
        runner: CommandRunner | None = None,
        expected_uid: int = 0,
        expected_gid: int = 0,
        effective_uid: int | None = None,
        invocation_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.source_root = source_root
        self.etc_root = etc_root
        self.systemd_root = systemd_root
        self.local_sbin_root = local_sbin_root
        self.docker_root = docker_root
        self.releases_root = releases_root
        self.state_path = state_path or etc_root / STATE_PATH.name
        self.runner = runner or SubprocessRunner()
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        environment = (
            os.environ if invocation_environment is None else invocation_environment
        )
        self.raw_sudo_uid = environment.get("SUDO_UID")
        self.assets = build_asset_specs(
            source_root=source_root,
            etc_root=etc_root,
            systemd_root=systemd_root,
            local_sbin_root=local_sbin_root,
        )

    def _git(self, *arguments: str, git_sudo_uid: str | None) -> CommandResult:
        return self.runner.run(
            (
                GIT_BINARY,
                "-C",
                str(self.source_root),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *arguments,
            ),
            git_sudo_uid=git_sudo_uid,
        )

    def _source_snapshots(self, expected_commit: str) -> tuple[AssetSnapshot, ...]:
        """Bind every working-tree asset to the clean expected Git commit."""

        if COMMIT_RE.fullmatch(expected_commit) is None:
            raise HostAssetInstallError(
                "expected commit must be a lowercase 40-character SHA"
            )
        try:
            _assert_existing_path_chain_no_symlinks(self.source_root)
            source_metadata = self.source_root.lstat()
        except HostAssetInstallError as exc:
            raise HostAssetInstallError("source root is unsafe") from exc
        except OSError as exc:
            raise HostAssetInstallError("source root is unsafe") from exc
        if not stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISLNK(
            source_metadata.st_mode
        ):
            raise HostAssetInstallError("source root is unsafe")
        git_sudo_uid = _validated_git_sudo_uid(
            source_owner_uid=source_metadata.st_uid,
            effective_uid=self.effective_uid,
            raw_sudo_uid=self.raw_sudo_uid,
        )
        head = self._git(
            "rev-parse", "--verify", "HEAD^{commit}", git_sudo_uid=git_sudo_uid
        )
        object_type = self._git(
            "cat-file", "-t", expected_commit, git_sudo_uid=git_sudo_uid
        )
        clean = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            git_sudo_uid=git_sudo_uid,
        )
        if (
            head.returncode != 0
            or head.stdout != f"{expected_commit}\n".encode("ascii")
            or object_type.returncode != 0
            or object_type.stdout != b"commit\n"
            or clean.returncode != 0
            or clean.stdout != b""
        ):
            raise HostAssetInstallError(
                "source checkout is not the clean expected commit"
            )

        snapshots: list[AssetSnapshot] = []
        for spec in self.assets:
            source = self.source_root / spec.source_relative
            payload, metadata = _read_regular_no_follow(source, MAX_ASSET_BYTES)
            if stat.S_IMODE(metadata.st_mode) != int(spec.git_mode[-3:], 8):
                raise HostAssetInstallError(
                    "source asset mode does not match the contract"
                )
            tree = self._git(
                "ls-tree",
                "-z",
                expected_commit,
                "--",
                spec.source_relative.as_posix(),
                git_sudo_uid=git_sudo_uid,
            )
            fields, separator, observed_path = tree.stdout.partition(b"\t")
            metadata_fields = fields.split()
            if (
                tree.returncode != 0
                or separator != b"\t"
                or observed_path
                != spec.source_relative.as_posix().encode("ascii") + b"\0"
                or len(metadata_fields) != 3
                or metadata_fields[0] != spec.git_mode.encode("ascii")
                or metadata_fields[1] != b"blob"
                or re.fullmatch(rb"[0-9a-f]{40,64}", metadata_fields[2]) is None
            ):
                raise HostAssetInstallError(
                    "source asset is not a regular file in the expected commit"
                )
            committed = self._git(
                "cat-file",
                "blob",
                f"{expected_commit}:{spec.source_relative.as_posix()}",
                git_sudo_uid=git_sudo_uid,
            )
            if committed.returncode != 0 or committed.stdout != payload:
                raise HostAssetInstallError(
                    "source asset bytes do not match the expected commit"
                )
            snapshots.append(
                AssetSnapshot(
                    spec=spec,
                    payload=payload,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        return tuple(snapshots)

    def _require_storage_ready(self, snapshots: Sequence[AssetSnapshot]) -> None:
        snapshot = next(
            (item for item in snapshots if item.spec.name == "storage-preflight"),
            None,
        )
        if snapshot is None or len(snapshot.payload) > MAX_INLINE_PREFLIGHT_BYTES:
            raise HostAssetInstallError(
                "verified production storage preflight is unavailable"
            )
        try:
            verified_source = snapshot.payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HostAssetInstallError(
                "verified production storage preflight is unavailable"
            ) from exc
        if "\x00" in verified_source:
            raise HostAssetInstallError(
                "verified production storage preflight is unavailable"
            )
        result = self.runner.run(
            (
                PYTHON_BINARY,
                "-I",
                "-c",
                verified_source,
                "--mode",
                "startup",
            )
        )
        if result.returncode != 0:
            raise HostAssetInstallError("production storage preflight did not pass")

    def _require_empty_docker_root(self) -> None:
        result = self.runner.run(
            (
                FINDMNT_BINARY,
                "--noheadings",
                "--output",
                "FSTYPE",
                "--target",
                str(self.docker_root),
            )
        )
        try:
            filesystem = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeError as exc:
            raise HostAssetInstallError(
                "Docker root filesystem is unavailable"
            ) from exc
        if result.returncode != 0 or filesystem not in {"ext4", "xfs"}:
            raise HostAssetInstallError("Docker root filesystem is unavailable")
        _validate_empty_docker_root(
            self.docker_root,
            filesystem=filesystem,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )

    def _require_docker_units_masked_inactive(self) -> None:
        for unit in ("docker.service", "docker.socket", "containerd.service"):
            load = self.runner.run(
                (
                    SYSTEMCTL_BINARY,
                    "show",
                    "--property=LoadState",
                    "--value",
                    unit,
                )
            )
            active = self.runner.run((SYSTEMCTL_BINARY, "is-active", unit))
            if (
                load.returncode != 0
                or load.stdout.strip() != b"masked"
                or active.returncode != 3
                or active.stdout.strip() != b"inactive"
            ):
                raise HostAssetInstallError(
                    "Docker service, socket, and containerd must be masked and inactive"
                )

    def _require_services_inactive(self) -> None:
        self._require_docker_units_masked_inactive()

        platform_load = self.runner.run(
            (
                SYSTEMCTL_BINARY,
                "show",
                "--property=LoadState",
                "--value",
                "sms-platform.service",
            )
        )
        platform_active = self.runner.run(
            (SYSTEMCTL_BINARY, "is-active", "sms-platform.service")
        )
        platform_absent = (
            platform_load.returncode == 0
            and platform_load.stdout.strip() == b"not-found"
            and platform_active.returncode in {3, 4}
            and platform_active.stdout.strip() in {b"inactive", b"unknown"}
        )
        if not platform_absent:
            raise HostAssetInstallError(
                "the not-yet-installed platform unit must be absent"
            )

    def _require_upgrade_prebootstrap(self) -> None:
        """Prove the completed host install has not crossed first bootstrap."""

        self._require_docker_units_masked_inactive()
        expected_enablement = {
            "sms-platform.service": (b"disabled", 1),
            "vendor-control-agent.service": (b"disabled", 1),
            "sms-partition-maintenance.timer": (b"disabled", 1),
            "sms-backup.timer": (b"disabled", 1),
            "sms-restore-drill.timer": (b"static", 0),
            "sms-lifecycle-status.timer": (b"disabled", 1),
        }
        for unit, (
            expected_enabled_state,
            expected_enabled_code,
        ) in expected_enablement.items():
            active = self.runner.run((SYSTEMCTL_BINARY, "is-active", unit))
            enabled = self.runner.run((SYSTEMCTL_BINARY, "is-enabled", unit))
            if (
                active.returncode != 3
                or active.stdout.strip() != b"inactive"
                or enabled.returncode != expected_enabled_code
                or enabled.stdout.strip() != expected_enabled_state
            ):
                raise HostAssetInstallError(
                    "platform services and maintenance timers must remain pre-bootstrap"
                )
        for unit in (
            "sms-partition-maintenance.service",
            "sms-backup.service",
            "sms-restore-drill.service",
            "sms-lifecycle-status.service",
        ):
            active = self.runner.run((SYSTEMCTL_BINARY, "is-active", unit))
            if active.returncode != 3 or active.stdout.strip() != b"inactive":
                raise HostAssetInstallError(
                    "maintenance services must remain inactive before bootstrap"
                )
        if _lexists(self.releases_root):
            _assert_existing_path_chain_no_symlinks(self.releases_root)
            _validate_existing_directory(
                self.releases_root,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            try:
                with os.scandir(self.releases_root) as entries:
                    if next(entries, None) is not None:
                        raise HostAssetInstallError(
                            "production release root must be empty before bootstrap"
                        )
            except OSError as exc:
                raise HostAssetInstallError(
                    "production release root is unavailable"
                ) from exc

    def _systemctl_property(self, unit: str, property_name: str) -> str:
        result = self.runner.run(
            (
                SYSTEMCTL_BINARY,
                "show",
                f"--property={property_name}",
                "--value",
                unit,
            )
        )
        try:
            value = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeError as exc:
            raise HostAssetInstallError(
                "systemd acceptance evidence is invalid"
            ) from exc
        if result.returncode != 0 or "\x00" in value or "\n" in value:
            raise HostAssetInstallError("systemd acceptance evidence is invalid")
        return value

    def _storage_invocation_id(self) -> str:
        value = self._systemctl_property(
            "sms-storage-preflight.service", "InvocationID"
        )
        if re.fullmatch(r"(?:|[0-9a-f]{32})", value) is None:
            raise HostAssetInstallError("storage preflight invocation is invalid")
        return value

    def _require_storage_preflight_quiescent(self, *, allow_failed: bool) -> None:
        """Require no running process or queued PID 1 job for the storage unit."""

        unit = "sms-storage-preflight.service"
        state = (
            self._systemctl_property(unit, "ActiveState"),
            self._systemctl_property(unit, "SubState"),
        )
        allowed_states = {("inactive", "dead")}
        if allow_failed:
            allowed_states.add(("failed", "failed"))
        if state not in allowed_states or self._systemctl_property(unit, "Job"):
            raise HostAssetInstallError(
                "storage preflight must be quiescent without a pending systemd job"
            )

    @staticmethod
    def _candidate_capability_set(snapshot: AssetSnapshot) -> set[str]:
        try:
            text = snapshot.payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise HostAssetInstallError("candidate systemd unit is invalid") from exc
        lines = [
            line
            for line in text.splitlines()
            if line.startswith("CapabilityBoundingSet=")
        ]
        if len(lines) != 1:
            raise HostAssetInstallError("candidate systemd unit is invalid")
        return {token.lower() for token in lines[0].partition("=")[2].split() if token}

    def _require_upgrade_acceptance(
        self,
        snapshots: Sequence[AssetSnapshot],
        changes: Sequence[UpgradeChange],
    ) -> None:
        """Verify the loaded units, then synchronously run the candidate preflight."""

        snapshot_by_name = {snapshot.spec.name: snapshot for snapshot in snapshots}
        for change in changes:
            snapshot = snapshot_by_name[change.snapshot.spec.name]
            destination = snapshot.spec.destination
            if destination.parent != self.systemd_root or destination.suffix not in {
                ".service",
                ".timer",
            }:
                continue
            unit = destination.name
            if self._systemctl_property(unit, "FragmentPath") != str(destination):
                raise HostAssetInstallError(
                    "candidate systemd unit fragment path is not loaded"
                )
            if self._systemctl_property(unit, "LoadState") != "loaded":
                raise HostAssetInstallError("candidate systemd unit is not loaded")
            if self._systemctl_property(unit, "NeedDaemonReload") != "no":
                raise HostAssetInstallError(
                    "candidate systemd unit still requires daemon-reload"
                )
            if destination.suffix == ".service":
                expected_capabilities = self._candidate_capability_set(snapshot)
                observed_capabilities = {
                    token.lower()
                    for token in self._systemctl_property(
                        unit, "CapabilityBoundingSet"
                    ).split()
                    if token
                }
                if observed_capabilities != expected_capabilities:
                    raise HostAssetInstallError(
                        "candidate systemd capability set is not loaded"
                    )
                if "SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL=1" not in (
                    self._systemctl_property(unit, "Environment").split()
                ):
                    raise HostAssetInstallError(
                        "candidate systemd credential marker is not loaded"
                    )

        storage_unit = "sms-storage-preflight.service"
        self._require_storage_preflight_quiescent(allow_failed=True)
        started = self.runner.run(
            (
                SYSTEMCTL_BINARY,
                "--system",
                "--no-ask-password",
                "--job-mode=fail",
                "start",
                storage_unit,
            ),
            timeout_seconds=UPGRADE_ACCEPTANCE_TIMEOUT_SECONDS,
        )
        if started.returncode != 0:
            raise HostAssetInstallError(
                "candidate storage preflight did not start successfully"
            )
        self._require_storage_preflight_quiescent(allow_failed=False)
        if (
            self._systemctl_property(storage_unit, "Result") != "success"
            or self._systemctl_property(storage_unit, "ExecMainStatus") != "0"
        ):
            raise HostAssetInstallError(
                "candidate storage preflight has not completed successfully"
            )

    def _require_empty_destinations(self) -> None:
        destinations = [snapshot.destination for snapshot in self.assets]
        destinations.append(self.state_path)
        if any(_lexists(destination) for destination in destinations):
            raise HostAssetInstallError("host asset destination already exists")

    def _validate_plan_directories(self) -> None:
        for path in (self.systemd_root, self.local_sbin_root):
            _assert_existing_path_chain_no_symlinks(path)
            _validate_existing_directory(
                path,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
        for path, mode in (
            (self.etc_root, 0o700),
            (self.systemd_root / "docker.service.d", 0o755),
            (self.systemd_root / "sms-platform.service.d", 0o755),
        ):
            if _lexists(path):
                _assert_existing_path_chain_no_symlinks(path)
                _validate_existing_directory(
                    path,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                    exact_mode=mode,
                )
            else:
                _assert_existing_path_chain_no_symlinks(path.parent)

    def _inspect_host_readiness(
        self, expected_commit: str
    ) -> tuple[AssetSnapshot, ...]:
        snapshots = self._source_snapshots(expected_commit)
        self._validate_plan_directories()
        self._require_storage_ready(snapshots)
        self._require_services_inactive()
        self._require_empty_docker_root()
        return snapshots

    def _inspect_plan(self, expected_commit: str) -> tuple[AssetSnapshot, ...]:
        snapshots = self._inspect_host_readiness(expected_commit)
        self._require_empty_destinations()
        return snapshots

    def _inspect_upgrade_readiness(
        self, expected_commit: str
    ) -> tuple[AssetSnapshot, ...]:
        snapshots = self._source_snapshots(expected_commit)
        self._validate_plan_directories()
        self._require_storage_ready(snapshots)
        self._require_upgrade_prebootstrap()
        self._require_empty_docker_root()
        return snapshots

    def _require_upgrade_runtime_boundary(self) -> None:
        """Recheck mutable pre-bootstrap state while holding the installer lock."""

        self._require_storage_preflight_quiescent(allow_failed=True)
        self._require_upgrade_prebootstrap()
        self._require_empty_docker_root()

    def _changes_from_manifests(
        self,
        state: InstalledState,
        snapshots: Sequence[AssetSnapshot],
    ) -> tuple[UpgradeChange, ...]:
        by_name = {snapshot.spec.name: snapshot for snapshot in snapshots}
        changes: list[UpgradeChange] = []
        for spec in self.assets:
            snapshot = by_name[spec.name]
            old_item = state.assets_by_name[spec.name]
            if old_item["sha256"] != snapshot.sha256:
                if spec.kind != "regular":
                    raise HostAssetInstallError(
                        "host asset upgrade cannot change the wrapper symlink target"
                    )
                changes.append(UpgradeChange(snapshot=snapshot, old_item=old_item))
        if not changes:
            raise HostAssetInstallError("host asset upgrade has no changed assets")
        if {change.snapshot.spec.name for change in changes} != set(
            PREBOOTSTRAP_REPAIR_ASSETS
        ):
            raise HostAssetInstallError(
                "pre-bootstrap repair must change exactly the reviewed six assets"
            )
        return tuple(changes)

    def _old_payloads_from_destinations(
        self, changes: Sequence[UpgradeChange]
    ) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for change in changes:
            spec = change.snapshot.spec
            if not self._destination_matches_state_item(spec, change.old_item):
                raise HostAssetInstallError("installed host asset has drifted")
            payload, _metadata = _read_regular_no_follow(
                spec.destination, MAX_ASSET_BYTES
            )
            payloads[spec.name] = payload
        return payloads

    @staticmethod
    def _capability_line(payload: bytes) -> bytes:
        lines = [
            line
            for line in payload.splitlines()
            if line.startswith(b"CapabilityBoundingSet=")
        ]
        if len(lines) != 1:
            raise HostAssetInstallError("systemd capability contract is invalid")
        return lines[0]

    def _validate_prebootstrap_repair_contract(
        self,
        changes: Sequence[UpgradeChange],
        old_payloads: Mapping[str, bytes],
    ) -> None:
        by_name = {change.snapshot.spec.name: change for change in changes}
        if set(by_name) != set(PREBOOTSTRAP_REPAIR_ASSETS):
            raise HostAssetInstallError("pre-bootstrap repair scope is invalid")
        for name in HOST_MOUNTINFO_UNIT_ASSETS:
            change = by_name[name]
            old_payload = old_payloads.get(name)
            if old_payload is None or hashlib.sha256(old_payload).hexdigest() != str(
                change.old_item["sha256"]
            ):
                raise HostAssetInstallError(
                    "pre-bootstrap repair old asset has drifted"
                )
            target = change.snapshot.payload
            if (
                b"CAP_SYS_PTRACE" in target
                or self._capability_line(target) != self._capability_line(old_payload)
                or target.splitlines().count(HOST_MOUNTINFO_CREDENTIAL_LINE) != 1
                or target.splitlines().count(HOST_MOUNTINFO_MARKER_LINE) != 1
            ):
                raise HostAssetInstallError(
                    "pre-bootstrap repair systemd credential contract is invalid"
                )
        script = by_name["storage-preflight"].snapshot.payload
        for token in (
            b"SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL",
            b"CREDENTIALS_DIRECTORY",
            b"sms-host-mountinfo",
        ):
            if token not in script:
                raise HostAssetInstallError(
                    "pre-bootstrap repair script credential contract is invalid"
                )
        if any(b"CAP_SYS_PTRACE" in change.snapshot.payload for change in changes):
            raise HostAssetInstallError(
                "pre-bootstrap repair cannot grant process-tracing capability"
            )

    def _require_upgrade_intent_absent(self) -> None:
        if _lexists(self._intent_path()) or _lexists(self._upgrade_intent_path()):
            raise HostAssetInstallError("another host asset transaction already exists")

    def _inspect_upgrade_plan(
        self, from_commit: str, expected_commit: str
    ) -> tuple[tuple[AssetSnapshot, ...], InstalledState, tuple[UpgradeChange, ...]]:
        if from_commit != PREBOOTSTRAP_REPAIR_FROM_COMMIT:
            raise HostAssetInstallError(
                "pre-bootstrap repair does not match its fixed source commit"
            )
        snapshots = self._inspect_upgrade_readiness(expected_commit)
        self._require_upgrade_intent_absent()
        state = self._validated_state_manifest()
        if state.commit != from_commit:
            raise HostAssetInstallError("installed state does not match from commit")
        self._require_state_destinations_exact(state)
        changes = self._changes_from_manifests(state, snapshots)
        self._validate_prebootstrap_repair_contract(
            changes, self._old_payloads_from_destinations(changes)
        )
        return snapshots, state, changes

    def plan(
        self, expected_commit: str, *, from_commit: str | None = None
    ) -> dict[str, object]:
        """Run only read paths and return the bounded installation plan."""

        if from_commit is not None:
            _snapshots, _state, changes = self._inspect_upgrade_plan(
                from_commit, expected_commit
            )
            return {
                "action": "plan",
                "assets": len(self.assets),
                "assets_changed": len(changes),
                "changed_assets": [change.snapshot.spec.name for change in changes],
                "from_commit": from_commit,
                "mode": "upgrade",
                "source_commit": expected_commit,
                "status": "ready",
            }
        self._inspect_plan(expected_commit)
        return {
            "action": "plan",
            "assets": len(self.assets),
            "source_commit": expected_commit,
            "status": "ready",
        }

    def _intent_path(self) -> Path:
        return self.etc_root / INTENT_NAME

    def _lock_path(self) -> Path:
        return self.etc_root / LOCK_NAME

    def _upgrade_intent_path(self) -> Path:
        return self.etc_root / UPGRADE_INTENT_NAME

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        file_obj = self._lock_path().open("a+")
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            file_obj.close()
            raise HostAssetInstallError("installer lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
            file_obj.close()

    def _intent_payload(
        self,
        expected_commit: str,
        snapshots: Sequence[AssetSnapshot],
        *,
        published: Sequence[str] | None = None,
        phase: str = "publishing",
    ) -> dict[str, object]:
        return {
            "schema_version": INTENT_SCHEMA_VERSION,
            "commit": expected_commit,
            "phase": phase,
            "published": list(published or []),
            "assets": [
                {
                    "destination": str(snapshot.spec.destination),
                    "kind": snapshot.spec.kind,
                    "mode": f"{snapshot.spec.mode:04o}",
                    "name": snapshot.spec.name,
                    "owner": f"{self.expected_uid}:{self.expected_gid}",
                    "sha256": snapshot.sha256,
                    **(
                        {"target": str(snapshot.spec.symlink_target)}
                        if snapshot.spec.symlink_target is not None
                        else {}
                    ),
                }
                for snapshot in snapshots
            ],
        }

    def _write_intent(self, intent: Mapping[str, object]) -> None:
        payload = (
            json.dumps(
                dict(intent),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        if len(payload) > MAX_INTENT_BYTES:
            raise HostAssetInstallError("install intent exceeds its size limit")
        temporary = _stage_regular(
            self._intent_path(),
            payload,
            mode=0o600,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        _commit_regular_replace(temporary, self._intent_path())

    def _load_intent(self) -> dict[str, object] | None:
        path = self._intent_path()
        if not _lexists(path):
            return None
        try:
            payload, metadata = _read_regular_no_follow(path, MAX_INTENT_BYTES)
        except HostAssetInstallError:
            return None
        if (
            metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return None
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != INTENT_SCHEMA_VERSION
            or not isinstance(value.get("commit"), str)
            or COMMIT_RE.fullmatch(str(value["commit"])) is None
            or not isinstance(value.get("assets"), list)
            or not value["assets"]
        ):
            return None
        published = value.get("published")
        if published is not None and not isinstance(published, list):
            return None
        return value

    def _clear_intent(self) -> None:
        path = self._intent_path()
        if _lexists(path):
            path.unlink()
            _fsync_directory(path.parent)

    def _published_names(self, intent: Mapping[str, object]) -> list[str]:
        published = intent.get("published")
        if not isinstance(published, list):
            return []
        return [str(name) for name in published]

    def _mark_published(
        self, intent: dict[str, object], name: str, *, phase: str = "publishing"
    ) -> dict[str, object]:
        published = self._published_names(intent)
        if name not in published:
            published.append(name)
        updated = {**intent, "phase": phase, "published": published}
        self._write_intent(updated)
        return updated

    def _upgrade_intent_payload(
        self,
        *,
        from_commit: str,
        expected_commit: str,
        previous_state_sha256: str,
        snapshots: Sequence[AssetSnapshot],
        changes: Sequence[UpgradeChange],
        backup_payloads: Mapping[str, bytes],
        storage_invocation_id_before: str,
    ) -> dict[str, object]:
        target_assets = self._intent_payload(expected_commit, snapshots)["assets"]
        old_by_name = {change.snapshot.spec.name: change.old_item for change in changes}
        previous_assets = []
        for item in target_assets:
            assert isinstance(item, dict)
            name = str(item["name"])
            previous_assets.append(
                dict(old_by_name[name])
                if name in old_by_name
                else {key: value for key, value in item.items() if key != "owner"}
            )
        return {
            "schema_version": UPGRADE_INTENT_SCHEMA_VERSION,
            "operation": "upgrade",
            "from_commit": from_commit,
            "commit": expected_commit,
            "previous_state_sha256": previous_state_sha256,
            "previous_assets": previous_assets,
            # Retained in the v1 intent for resume/rollback compatibility. A
            # fresh synchronous systemctl start is the acceptance proof.
            "storage_invocation_id_before": storage_invocation_id_before,
            "changes": [change.snapshot.spec.name for change in changes],
            "assets": target_assets,
            "backups": {
                name: base64.b64encode(payload).decode("ascii")
                for name, payload in backup_payloads.items()
            },
        }

    def _write_upgrade_intent(self, intent: Mapping[str, object]) -> None:
        payload = (
            json.dumps(
                dict(intent),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        if len(payload) > MAX_INTENT_BYTES:
            raise HostAssetInstallError("upgrade intent exceeds its size limit")
        temporary = _stage_regular(
            self._upgrade_intent_path(),
            payload,
            mode=0o600,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        _commit_regular_no_replace(temporary, self._upgrade_intent_path())

    def _upgrade_intent_stage_link(self, metadata: os.stat_result) -> Path | None:
        """Locate the sole safe staging hardlink left by an interrupted publish."""

        pattern = re.compile(
            rf"\.{re.escape(UPGRADE_INTENT_NAME)}\.[0-9a-f]{{32}}\.tmp"
        )
        matches: list[Path] = []
        try:
            with os.scandir(self.etc_root) as entries:
                for entry in entries:
                    if pattern.fullmatch(entry.name) is None:
                        continue
                    candidate = entry.stat(follow_symlinks=False)
                    if (
                        candidate.st_dev == metadata.st_dev
                        and candidate.st_ino == metadata.st_ino
                    ):
                        matches.append(self.etc_root / entry.name)
        except OSError as exc:
            raise HostAssetInstallError(
                "upgrade intent staging link is unsafe"
            ) from exc
        return matches[0] if len(matches) == 1 else None

    def _read_upgrade_intent_file(self) -> tuple[bytes, os.stat_result]:
        path = self._upgrade_intent_path()
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise HostAssetInstallError("upgrade intent is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink not in {1, 2}:
                raise HostAssetInstallError("upgrade intent is unsafe")
            if (
                metadata.st_nlink == 2
                and self._upgrade_intent_stage_link(metadata) is None
            ):
                raise HostAssetInstallError("upgrade intent staging link is unsafe")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(MAX_INTENT_BYTES + 1)
            if len(payload) > MAX_INTENT_BYTES:
                raise HostAssetInstallError("upgrade intent exceeds its size limit")
            return payload, metadata
        finally:
            os.close(descriptor)

    def _load_upgrade_intent(self) -> dict[str, object] | None:
        path = self._upgrade_intent_path()
        if not _lexists(path):
            return None
        try:
            payload, metadata = self._read_upgrade_intent_file()
        except HostAssetInstallError:
            return None
        if (
            metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return None
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != UPGRADE_INTENT_SCHEMA_VERSION
            or value.get("operation") != "upgrade"
            or not isinstance(value.get("from_commit"), str)
            or COMMIT_RE.fullmatch(str(value["from_commit"])) is None
            or not isinstance(value.get("commit"), str)
            or COMMIT_RE.fullmatch(str(value["commit"])) is None
            or not isinstance(value.get("previous_state_sha256"), str)
            or SHA256_RE.fullmatch(str(value["previous_state_sha256"])) is None
            or not isinstance(value.get("storage_invocation_id_before"), str)
            or re.fullmatch(
                r"(?:|[0-9a-f]{32})", str(value["storage_invocation_id_before"])
            )
            is None
            or not isinstance(value.get("changes"), list)
            or not value["changes"]
            or not isinstance(value.get("previous_assets"), list)
            or not isinstance(value.get("assets"), list)
            or not isinstance(value.get("backups"), dict)
        ):
            return None
        changes = value["changes"]
        if any(not isinstance(name, str) for name in changes) or len(
            set(changes)
        ) != len(changes):
            return None
        return value

    def _destination_matches_snapshot(self, snapshot: AssetSnapshot) -> bool:
        destination = snapshot.spec.destination
        if not _lexists(destination):
            return False
        if snapshot.spec.kind == "symlink":
            try:
                metadata = destination.lstat()
            except OSError:
                return False
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != self.expected_gid
                or os.readlink(destination) != str(snapshot.spec.symlink_target)
            ):
                return False
            try:
                _validate_wrapper_target(snapshot.spec, snapshot.sha256)
            except HostAssetInstallError:
                return False
            return True
        try:
            payload, metadata = _read_regular_no_follow(destination, MAX_ASSET_BYTES)
        except HostAssetInstallError:
            return False
        return (
            hashlib.sha256(payload).hexdigest() == snapshot.sha256
            and metadata.st_uid == self.expected_uid
            and metadata.st_gid == self.expected_gid
            and stat.S_IMODE(metadata.st_mode) == snapshot.spec.mode
        )

    def _validate_upgrade_intent_contract(
        self,
        intent: Mapping[str, object],
        *,
        from_commit: str,
        expected_commit: str,
        previous_state_sha256: str,
        snapshots: Sequence[AssetSnapshot],
        changes: Sequence[UpgradeChange],
    ) -> None:
        expected_assets = self._intent_payload(expected_commit, snapshots)["assets"]
        expected_changes = [change.snapshot.spec.name for change in changes]
        old_by_name = {change.snapshot.spec.name: change.old_item for change in changes}
        expected_previous_assets = [
            dict(old_by_name[str(item["name"])])
            if str(item["name"]) in old_by_name
            else {key: value for key, value in item.items() if key != "owner"}
            for item in expected_assets
            if isinstance(item, dict)
        ]
        if (
            intent.get("from_commit") != from_commit
            or intent.get("commit") != expected_commit
            or intent.get("previous_state_sha256") != previous_state_sha256
            or intent.get("previous_assets") != expected_previous_assets
            or intent.get("assets") != expected_assets
            or intent.get("changes") != expected_changes
        ):
            raise HostAssetInstallError(
                "host asset upgrade intent does not match the exact old and new commits"
            )

    def _upgrade_destination_states(
        self,
        state: InstalledState,
        snapshots: Sequence[AssetSnapshot],
        changes: Sequence[UpgradeChange],
    ) -> dict[str, str]:
        changed_by_name = {change.snapshot.spec.name: change for change in changes}
        states: dict[str, str] = {}
        for snapshot in snapshots:
            name = snapshot.spec.name
            change = changed_by_name.get(name)
            if change is None:
                if not self._destination_matches_snapshot(snapshot):
                    raise HostAssetInstallError("unchanged host asset has drifted")
                states[name] = "unchanged"
                continue
            if self._destination_matches_state_item(snapshot.spec, change.old_item):
                states[name] = "old"
            elif self._destination_matches_snapshot(snapshot):
                states[name] = "new"
            else:
                raise HostAssetInstallError("changed host asset is neither old nor new")
        return states

    def _decode_upgrade_backups(
        self,
        intent: Mapping[str, object],
        changes: Sequence[UpgradeChange],
    ) -> dict[str, bytes]:
        raw_backups = intent.get("backups")
        if not isinstance(raw_backups, dict) or set(raw_backups) != {
            change.snapshot.spec.name for change in changes
        }:
            raise HostAssetInstallError("host asset upgrade backups are invalid")
        payloads: dict[str, bytes] = {}
        for change in changes:
            name = change.snapshot.spec.name
            encoded = raw_backups.get(name)
            if not isinstance(encoded, str):
                raise HostAssetInstallError("host asset upgrade backups are invalid")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HostAssetInstallError(
                    "host asset upgrade backups are invalid"
                ) from exc
            if (
                len(payload) > MAX_ASSET_BYTES
                or hashlib.sha256(payload).hexdigest() != change.old_item["sha256"]
            ):
                raise HostAssetInstallError("host asset upgrade backups are invalid")
            payloads[name] = payload
        return payloads

    def _require_exact_target_assets(self, snapshots: Sequence[AssetSnapshot]) -> None:
        if any(
            not self._destination_matches_snapshot(snapshot) for snapshot in snapshots
        ):
            raise HostAssetInstallError("host asset upgrade target assets have drifted")

    def _clear_upgrade_transaction(self) -> None:
        path = self._upgrade_intent_path()
        if _lexists(path):
            metadata = path.lstat()
            if metadata.st_nlink == 2:
                stage_link = self._upgrade_intent_stage_link(metadata)
                if stage_link is None:
                    raise HostAssetInstallError("upgrade intent staging link is unsafe")
                stage_link.unlink()
                _fsync_directory(path.parent)
            path.unlink()
            _fsync_directory(path.parent)

    def _validate_upgrade_status(
        self,
        state: InstalledState,
        intent: Mapping[str, object],
    ) -> None:
        from_commit = intent.get("from_commit")
        target_commit = intent.get("commit")
        raw_targets = intent.get("assets")
        raw_previous = intent.get("previous_assets")
        raw_changes = intent.get("changes")
        if (
            from_commit != PREBOOTSTRAP_REPAIR_FROM_COMMIT
            or not isinstance(target_commit, str)
            or COMMIT_RE.fullmatch(target_commit) is None
            or not isinstance(raw_targets, list)
            or not isinstance(raw_previous, list)
            or not isinstance(raw_changes, list)
            or set(raw_changes) != set(PREBOOTSTRAP_REPAIR_ASSETS)
            or _lexists(self._intent_path())
        ):
            raise HostAssetInstallError("host asset upgrade status is invalid")
        targets = {
            str(item.get("name")): item
            for item in raw_targets
            if isinstance(item, dict)
        }
        previous = {
            str(item.get("name")): item
            for item in raw_previous
            if isinstance(item, dict)
        }
        if len(targets) != len(self.assets) or len(previous) != len(self.assets):
            raise HostAssetInstallError("host asset upgrade status is invalid")
        for spec in self.assets:
            target = targets.get(spec.name)
            old = previous.get(spec.name)
            if not isinstance(target, Mapping) or not isinstance(old, Mapping):
                raise HostAssetInstallError("host asset upgrade status is invalid")
            target_sha = target.get("sha256")
            old_sha = old.get("sha256")
            if (
                not isinstance(target_sha, str)
                or SHA256_RE.fullmatch(target_sha) is None
                or not isinstance(old_sha, str)
                or SHA256_RE.fullmatch(old_sha) is None
            ):
                raise HostAssetInstallError("host asset upgrade status is invalid")
            target_expected: dict[str, object] = {
                "destination": str(spec.destination),
                "kind": spec.kind,
                "mode": f"{spec.mode:04o}",
                "name": spec.name,
                "owner": f"{self.expected_uid}:{self.expected_gid}",
                "sha256": target_sha,
            }
            old_expected = {
                key: value for key, value in target_expected.items() if key != "owner"
            }
            old_expected["sha256"] = old_sha
            if spec.symlink_target is not None:
                target_expected["target"] = str(spec.symlink_target)
                old_expected["target"] = str(spec.symlink_target)
            if target != target_expected or old != old_expected:
                raise HostAssetInstallError("host asset upgrade status is invalid")
        old_payload = (
            json.dumps(
                {
                    "assets": raw_previous,
                    "schema_version": STATE_SCHEMA_VERSION,
                    "source_commit": from_commit,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        if (
            intent.get("previous_state_sha256")
            != hashlib.sha256(old_payload).hexdigest()
        ):
            raise HostAssetInstallError("host asset upgrade status is invalid")
        observed_changes = {
            spec.name
            for spec in self.assets
            if previous[spec.name]["sha256"] != targets[spec.name]["sha256"]
        }
        if observed_changes != set(PREBOOTSTRAP_REPAIR_ASSETS):
            raise HostAssetInstallError("host asset upgrade status is invalid")
        raw_backups = intent.get("backups")
        if not isinstance(raw_backups, dict) or set(raw_backups) != observed_changes:
            raise HostAssetInstallError("host asset upgrade status is invalid")
        for name in observed_changes:
            encoded = raw_backups.get(name)
            if not isinstance(encoded, str):
                raise HostAssetInstallError("host asset upgrade status is invalid")
            try:
                backup = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HostAssetInstallError(
                    "host asset upgrade status is invalid"
                ) from exc
            if hashlib.sha256(backup).hexdigest() != previous[name]["sha256"]:
                raise HostAssetInstallError("host asset upgrade status is invalid")
        if state.commit == from_commit:
            if state.payload != old_payload:
                raise HostAssetInstallError("host asset upgrade status is invalid")
            for spec in self.assets:
                if spec.name not in observed_changes:
                    if not self._destination_matches_state_item(
                        spec, previous[spec.name]
                    ):
                        raise HostAssetInstallError(
                            "host asset upgrade status is invalid"
                        )
                elif not (
                    self._destination_matches_state_item(spec, previous[spec.name])
                    or self._destination_matches_state_item(spec, targets[spec.name])
                ):
                    raise HostAssetInstallError("host asset upgrade status is invalid")
        elif state.commit == target_commit:
            for spec in self.assets:
                canonical = {
                    key: value
                    for key, value in targets[spec.name].items()
                    if key != "owner"
                }
                if state.assets_by_name[spec.name] != canonical or not (
                    self._destination_matches_state_item(spec, targets[spec.name])
                ):
                    raise HostAssetInstallError("host asset upgrade status is invalid")
        else:
            raise HostAssetInstallError("host asset upgrade status is invalid")

    def _reject_mismatched_existing(self, snapshots: Sequence[AssetSnapshot]) -> None:
        for snapshot in snapshots:
            if not _lexists(snapshot.spec.destination):
                continue
            if not self._destination_matches_snapshot(snapshot):
                raise HostAssetInstallError("host asset destination already exists")

    def _create_destination_directories(self) -> None:
        for path, mode in (
            (self.etc_root, 0o700),
            (self.systemd_root / "docker.service.d", 0o755),
            (self.systemd_root / "sms-platform.service.d", 0o755),
        ):
            _create_fixed_directory(
                path,
                mode=mode,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )

    @staticmethod
    def _upgrade_result(
        *,
        action: str,
        status: str,
        from_commit: str,
        expected_commit: str,
        changes: Sequence[UpgradeChange],
    ) -> dict[str, object]:
        return {
            "action": action,
            "assets_changed": len(changes),
            "changed_assets": [change.snapshot.spec.name for change in changes],
            "from_commit": from_commit,
            "mode": "upgrade",
            "source_commit": expected_commit,
            "status": status,
        }

    def _upgrade_or_resume(
        self,
        *,
        from_commit: str,
        expected_commit: str,
        snapshots: Sequence[AssetSnapshot],
        allow_fresh: bool,
        action: str,
    ) -> dict[str, object]:
        if from_commit != PREBOOTSTRAP_REPAIR_FROM_COMMIT:
            raise HostAssetInstallError(
                "pre-bootstrap repair does not match its fixed source commit"
            )
        self._require_upgrade_runtime_boundary()
        intent = self._load_upgrade_intent()
        if _lexists(self._upgrade_intent_path()) and intent is None:
            raise HostAssetInstallError("host asset upgrade intent is invalid")
        if _lexists(self._intent_path()):
            raise HostAssetInstallError("another host asset transaction already exists")
        state = self._validated_state_manifest()
        if state.commit == expected_commit:
            if intent is None:
                raise HostAssetInstallError("completed upgrade intent is missing")
            self._validate_upgrade_status(state, intent)
            raw_changes = intent.get("changes")
            assert isinstance(raw_changes, list)
            self._clear_upgrade_transaction()
            result = self.status()
            if result.get("status") != "installed":
                raise HostAssetInstallError("completed host asset upgrade has drifted")
            return {
                "action": action,
                "assets_changed": len(raw_changes),
                "changed_assets": list(raw_changes),
                "from_commit": from_commit,
                "mode": "upgrade",
                "source_commit": expected_commit,
                "status": "installed",
            }
        if state.commit != from_commit:
            raise HostAssetInstallError("installed state does not match from commit")
        changes = self._changes_from_manifests(state, snapshots)
        state_sha256 = hashlib.sha256(state.payload).hexdigest()
        if intent is None:
            if not allow_fresh:
                raise HostAssetInstallError("host asset upgrade intent is missing")
            self._require_state_destinations_exact(state)
            backup_payloads = self._old_payloads_from_destinations(changes)
            self._validate_prebootstrap_repair_contract(changes, backup_payloads)
            intent = self._upgrade_intent_payload(
                from_commit=from_commit,
                expected_commit=expected_commit,
                previous_state_sha256=state_sha256,
                snapshots=snapshots,
                changes=changes,
                backup_payloads=backup_payloads,
                storage_invocation_id_before=self._storage_invocation_id(),
            )
            self._write_upgrade_intent(intent)
        else:
            self._validate_upgrade_intent_contract(
                intent,
                from_commit=from_commit,
                expected_commit=expected_commit,
                previous_state_sha256=state_sha256,
                snapshots=snapshots,
                changes=changes,
            )
            if intent.get("previous_state_sha256") != state_sha256:
                raise HostAssetInstallError("installed state changed during upgrade")

        backup_payloads = self._decode_upgrade_backups(intent, changes)
        self._validate_prebootstrap_repair_contract(changes, backup_payloads)
        destination_states = self._upgrade_destination_states(state, snapshots, changes)
        if all(
            destination_states[change.snapshot.spec.name] == "new" for change in changes
        ):
            self._require_exact_target_assets(snapshots)
            return self._upgrade_result(
                action=action,
                status="awaiting_acceptance",
                from_commit=from_commit,
                expected_commit=expected_commit,
                changes=changes,
            )

        for change in changes:
            spec = change.snapshot.spec
            if destination_states[spec.name] == "new":
                continue
            if destination_states[spec.name] != "old" or not (
                self._destination_matches_state_item(spec, change.old_item)
            ):
                raise HostAssetInstallError("changed host asset has drifted")
            temporary = _stage_regular(
                spec.destination,
                change.snapshot.payload,
                mode=spec.mode,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            try:
                if not self._destination_matches_state_item(spec, change.old_item):
                    raise HostAssetInstallError("changed host asset has drifted")
                _commit_regular_replace(temporary, spec.destination)
            finally:
                temporary.unlink(missing_ok=True)
        self._require_exact_target_assets(snapshots)
        return self._upgrade_result(
            action=action,
            status="awaiting_acceptance",
            from_commit=from_commit,
            expected_commit=expected_commit,
            changes=changes,
        )

    def upgrade_accept(
        self, expected_commit: str, *, from_commit: str
    ) -> dict[str, object]:
        """Commit the new state only after external systemd acceptance evidence."""

        if self.effective_uid != 0:
            raise HostAssetInstallError("upgrade acceptance requires root")
        if from_commit != PREBOOTSTRAP_REPAIR_FROM_COMMIT:
            raise HostAssetInstallError(
                "pre-bootstrap repair does not match its fixed source commit"
            )
        snapshots = self._inspect_upgrade_readiness(expected_commit)
        with self._exclusive_lock():
            intent = self._load_upgrade_intent()
            if intent is None:
                raise HostAssetInstallError("host asset upgrade intent is missing")
            state = self._validated_state_manifest()
            if state.commit == expected_commit:
                return self._upgrade_or_resume(
                    from_commit=from_commit,
                    expected_commit=expected_commit,
                    snapshots=snapshots,
                    allow_fresh=False,
                    action="upgrade-accept",
                )
            if state.commit != from_commit:
                raise HostAssetInstallError(
                    "installed state does not match from commit"
                )
            changes = self._changes_from_manifests(state, snapshots)
            self._validate_upgrade_intent_contract(
                intent,
                from_commit=from_commit,
                expected_commit=expected_commit,
                previous_state_sha256=hashlib.sha256(state.payload).hexdigest(),
                snapshots=snapshots,
                changes=changes,
            )
            if (
                intent.get("previous_state_sha256")
                != hashlib.sha256(state.payload).hexdigest()
            ):
                raise HostAssetInstallError("installed state changed during upgrade")
            destination_states = self._upgrade_destination_states(
                state, snapshots, changes
            )
            if any(
                destination_states[change.snapshot.spec.name] != "new"
                for change in changes
            ):
                raise HostAssetInstallError("host asset upgrade is incomplete")
            backup_payloads = self._decode_upgrade_backups(intent, changes)
            self._validate_prebootstrap_repair_contract(changes, backup_payloads)
            self._require_upgrade_acceptance(
                snapshots,
                changes,
            )
            self._require_upgrade_runtime_boundary()
            self._require_exact_target_assets(snapshots)
            temporary = _stage_regular(
                self.state_path,
                _state_payload(expected_commit, snapshots),
                mode=0o600,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            try:
                _commit_regular_replace(temporary, self.state_path)
            finally:
                temporary.unlink(missing_ok=True)
            new_state = self._validated_state_manifest()
            if new_state.commit != expected_commit:
                raise HostAssetInstallError("new host asset state was not committed")
            self._require_state_destinations_exact(new_state)
            self._clear_upgrade_transaction()
        result = self.status()
        if (
            result.get("status") != "installed"
            or result.get("source_commit") != expected_commit
        ):
            raise HostAssetInstallError("accepted host asset upgrade has drifted")
        return self._upgrade_result(
            action="upgrade-accept",
            status="installed",
            from_commit=from_commit,
            expected_commit=expected_commit,
            changes=changes,
        )

    def _rollback_upgrade(
        self, *, from_commit: str, expected_commit: str
    ) -> dict[str, object]:
        if from_commit != PREBOOTSTRAP_REPAIR_FROM_COMMIT:
            raise HostAssetInstallError(
                "pre-bootstrap repair does not match its fixed source commit"
            )
        snapshots = self._inspect_upgrade_readiness(expected_commit)
        with self._exclusive_lock():
            self._require_upgrade_runtime_boundary()
            intent = self._load_upgrade_intent()
            if intent is None:
                raise HostAssetInstallError("host asset upgrade intent is missing")
            state = self._validated_state_manifest()
            if state.commit == expected_commit:
                raise HostAssetInstallError("completed upgrade cannot be rolled back")
            if state.commit != from_commit:
                raise HostAssetInstallError(
                    "installed state does not match from commit"
                )
            changes = self._changes_from_manifests(state, snapshots)
            self._validate_upgrade_intent_contract(
                intent,
                from_commit=from_commit,
                expected_commit=expected_commit,
                previous_state_sha256=hashlib.sha256(state.payload).hexdigest(),
                snapshots=snapshots,
                changes=changes,
            )
            if (
                intent.get("previous_state_sha256")
                != hashlib.sha256(state.payload).hexdigest()
            ):
                raise HostAssetInstallError("installed state changed during upgrade")
            self._upgrade_destination_states(state, snapshots, changes)
            backups = self._decode_upgrade_backups(intent, changes)
            self._validate_prebootstrap_repair_contract(changes, backups)
            restored = 0
            for change in changes:
                spec = change.snapshot.spec
                if self._destination_matches_state_item(spec, change.old_item):
                    continue
                if not self._destination_matches_snapshot(change.snapshot):
                    raise HostAssetInstallError("changed host asset has drifted")
                temporary = _stage_regular(
                    spec.destination,
                    backups[spec.name],
                    mode=spec.mode,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                )
                try:
                    if not self._destination_matches_snapshot(change.snapshot):
                        raise HostAssetInstallError("changed host asset has drifted")
                    _commit_regular_replace(temporary, spec.destination)
                finally:
                    temporary.unlink(missing_ok=True)
                restored += 1
            self._require_state_destinations_exact(state)
            self._clear_upgrade_transaction()
        result = self.status()
        if (
            result.get("status") != "installed"
            or result.get("source_commit") != from_commit
        ):
            raise HostAssetInstallError("rolled back host asset upgrade has drifted")
        return {
            "action": "rollback",
            "assets_restored": restored,
            "from_commit": from_commit,
            "mode": "upgrade",
            "source_commit": from_commit,
            "status": "rolled_back",
            "target_commit": expected_commit,
        }

    def apply(
        self,
        expected_commit: str,
        *,
        confirm_dedicated_production_host: bool,
        confirm_vcenter_storage_reviewed: bool,
        from_commit: str | None = None,
    ) -> dict[str, object]:
        """Install only fixed assets; never prepare disks, secrets, or services."""

        if self.effective_uid != 0:
            raise HostAssetInstallError("apply requires root")
        if (
            not confirm_dedicated_production_host
            or not confirm_vcenter_storage_reviewed
        ):
            raise HostAssetInstallError(
                "both production host confirmations are required"
            )
        if from_commit is not None:
            snapshots = self._inspect_upgrade_readiness(expected_commit)
            with self._exclusive_lock():
                return self._upgrade_or_resume(
                    from_commit=from_commit,
                    expected_commit=expected_commit,
                    snapshots=snapshots,
                    allow_fresh=True,
                    action="apply",
                )
        snapshots = self._inspect_host_readiness(expected_commit)
        self._create_destination_directories()
        with self._exclusive_lock():
            return self._install_or_resume(
                expected_commit, snapshots, allow_fresh=True, action="apply"
            )

    def resume(
        self, expected_commit: str, *, from_commit: str | None = None
    ) -> dict[str, object]:
        """Continue a durable install intent without overwriting drifted destinations."""

        if self.effective_uid != 0:
            raise HostAssetInstallError("resume requires root")
        if from_commit is not None:
            snapshots = self._inspect_upgrade_readiness(expected_commit)
            with self._exclusive_lock():
                return self._upgrade_or_resume(
                    from_commit=from_commit,
                    expected_commit=expected_commit,
                    snapshots=snapshots,
                    allow_fresh=False,
                    action="resume",
                )
        snapshots = self._inspect_host_readiness(expected_commit)
        self._create_destination_directories()
        with self._exclusive_lock():
            return self._install_or_resume(
                expected_commit, snapshots, allow_fresh=False, action="resume"
            )

    def rollback(
        self,
        expected_commit: str,
        *,
        confirm_rollback_this_install: bool,
        from_commit: str | None = None,
    ) -> dict[str, object]:
        """Delete only destinations that still match this install intent."""

        if self.effective_uid != 0:
            raise HostAssetInstallError("rollback requires root")
        if not confirm_rollback_this_install:
            raise HostAssetInstallError("rollback confirmation is required")
        if from_commit is not None:
            return self._rollback_upgrade(
                from_commit=from_commit,
                expected_commit=expected_commit,
            )
        if COMMIT_RE.fullmatch(expected_commit) is None:
            raise HostAssetInstallError(
                "expected commit must be a lowercase 40-character SHA"
            )
        self.etc_root.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            if _lexists(self.state_path):
                raise HostAssetInstallError(
                    "completed installation cannot be rolled back"
                )
            intent = self._load_intent()
            if intent is None or str(intent.get("commit")) != expected_commit:
                raise HostAssetInstallError(
                    "install intent is missing or does not match"
                )
            snapshots = {
                snapshot.spec.name: snapshot
                for snapshot in self._source_snapshots(expected_commit)
            }
            removed = 0
            for spec in self.assets:
                if not _lexists(spec.destination):
                    continue
                snapshot = snapshots.get(spec.name)
                if snapshot is None or not self._destination_matches_snapshot(snapshot):
                    continue
                spec.destination.unlink()
                _fsync_directory(spec.destination.parent)
                removed += 1
            self._clear_intent()
            return {
                "action": "rollback",
                "assets_removed": removed,
                "source_commit": expected_commit,
                "status": "rolled_back",
            }

    def _install_or_resume(
        self,
        expected_commit: str,
        snapshots: Sequence[AssetSnapshot],
        *,
        allow_fresh: bool,
        action: str,
    ) -> dict[str, object]:
        intent = self._load_intent()
        if intent is None:
            if not allow_fresh:
                raise HostAssetInstallError("install intent is missing")
            self._require_empty_destinations()
            intent = self._intent_payload(expected_commit, snapshots)
            self._write_intent(intent)
        elif str(intent.get("commit")) != expected_commit:
            raise HostAssetInstallError("install intent does not match expected commit")
        else:
            self._reject_mismatched_existing(snapshots)

        staged: list[tuple[Path, AssetSnapshot]] = []
        state_temporary: Path | None = None
        try:
            for snapshot in snapshots:
                if snapshot.spec.kind != "regular":
                    continue
                if self._destination_matches_snapshot(snapshot):
                    intent = self._mark_published(intent, snapshot.spec.name)
                    continue
                if _lexists(snapshot.spec.destination):
                    raise HostAssetInstallError("host asset destination already exists")
                staged.append(
                    (
                        _stage_regular(
                            snapshot.spec.destination,
                            snapshot.payload,
                            mode=snapshot.spec.mode,
                            expected_uid=self.expected_uid,
                            expected_gid=self.expected_gid,
                        ),
                        snapshot,
                    )
                )
            state_temporary = _stage_regular(
                self.state_path,
                _state_payload(expected_commit, snapshots),
                mode=0o600,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            for temporary, snapshot in staged:
                _commit_regular_no_replace(temporary, snapshot.spec.destination)
                intent = self._mark_published(intent, snapshot.spec.name)
            wrapper_snapshot = next(
                snapshot for snapshot in snapshots if snapshot.spec.kind == "symlink"
            )
            if self._destination_matches_snapshot(wrapper_snapshot):
                intent = self._mark_published(
                    intent, wrapper_snapshot.spec.name, phase="state"
                )
            elif _lexists(wrapper_snapshot.spec.destination):
                raise HostAssetInstallError("host asset destination already exists")
            else:
                _validate_wrapper_target(wrapper_snapshot.spec, wrapper_snapshot.sha256)
                _commit_symlink_no_replace(
                    wrapper_snapshot.spec,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                )
                _validate_installed_wrapper(
                    wrapper_snapshot.spec,
                    wrapper_snapshot.sha256,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                )
                intent = self._mark_published(
                    intent, wrapper_snapshot.spec.name, phase="state"
                )
            _commit_regular_no_replace(state_temporary, self.state_path)
            state_temporary = None
            self._clear_intent()
        finally:
            for temporary, _snapshot in staged:
                temporary.unlink(missing_ok=True)
            if state_temporary is not None:
                state_temporary.unlink(missing_ok=True)
        installed_status = self.status()
        if (
            installed_status.get("status") != "installed"
            or installed_status.get("source_commit") != expected_commit
        ):
            raise HostAssetInstallError(
                "installed host assets failed post-publish verification"
            )
        return {
            "action": action,
            "assets": len(snapshots),
            "source_commit": expected_commit,
            "status": "installed",
        }

    def _read_state(self) -> dict[str, object]:
        payload, metadata = _read_regular_no_follow(self.state_path, MAX_STATE_BYTES)
        if (
            metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise HostAssetInstallError("host asset state file is unsafe")
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HostAssetInstallError("host asset state file is invalid") from exc
        if not isinstance(value, dict):
            raise HostAssetInstallError("host asset state file is invalid")
        return value

    def _validated_state_manifest(self) -> InstalledState:
        payload, metadata = _read_regular_no_follow(self.state_path, MAX_STATE_BYTES)
        if (
            metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise HostAssetInstallError("host asset state file is unsafe")
        try:
            state = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HostAssetInstallError("host asset state file is invalid") from exc
        if not isinstance(state, dict):
            raise HostAssetInstallError("host asset state file is invalid")
        commit = state.get("source_commit")
        raw_assets = state.get("assets")
        if (
            state.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(commit, str)
            or COMMIT_RE.fullmatch(commit) is None
            or not isinstance(raw_assets, list)
            or len(raw_assets) != len(self.assets)
        ):
            raise HostAssetInstallError("host asset state file is invalid")
        by_name: dict[str, Mapping[str, object]] = {}
        for item in raw_assets:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise HostAssetInstallError("host asset state file is invalid")
            by_name[item["name"]] = item
        if len(by_name) != len(self.assets):
            raise HostAssetInstallError("host asset state file is invalid")
        for spec in self.assets:
            item = by_name.get(spec.name)
            if item is None:
                raise HostAssetInstallError("host asset state file is invalid")
            item_sha256 = item.get("sha256")
            if (
                not isinstance(item_sha256, str)
                or SHA256_RE.fullmatch(item_sha256) is None
            ):
                raise HostAssetInstallError("host asset state file is invalid")
            expected_item: dict[str, object] = {
                "destination": str(spec.destination),
                "kind": spec.kind,
                "mode": f"{spec.mode:04o}",
                "name": spec.name,
                "sha256": item_sha256,
            }
            if spec.symlink_target is not None:
                expected_item["target"] = str(spec.symlink_target)
            if item != expected_item:
                raise HostAssetInstallError("host asset state file is invalid")
        return InstalledState(
            payload=payload,
            commit=commit,
            assets_by_name=by_name,
        )

    def _destination_matches_state_item(
        self, spec: AssetSpec, item: Mapping[str, object]
    ) -> bool:
        item_sha256 = item.get("sha256")
        if not isinstance(item_sha256, str) or SHA256_RE.fullmatch(item_sha256) is None:
            return False
        if spec.kind == "symlink":
            try:
                _validate_installed_wrapper(
                    spec,
                    item_sha256,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                )
            except HostAssetInstallError:
                return False
            return True
        try:
            payload, metadata = _read_regular_no_follow(
                spec.destination, MAX_ASSET_BYTES
            )
        except HostAssetInstallError:
            return False
        return (
            hashlib.sha256(payload).hexdigest() == item_sha256
            and metadata.st_uid == self.expected_uid
            and metadata.st_gid == self.expected_gid
            and stat.S_IMODE(metadata.st_mode) == spec.mode
        )

    def _require_state_destinations_exact(self, state: InstalledState) -> None:
        for spec in self.assets:
            item = state.assets_by_name[spec.name]
            if not self._destination_matches_state_item(spec, item):
                raise HostAssetInstallError("installed host asset has drifted")

    def status(self) -> dict[str, object]:
        """Inspect the commit-last state and every installed asset without mutation."""

        present = [asset for asset in self.assets if _lexists(asset.destination)]
        intent = self._load_intent()
        upgrade_intent = self._load_upgrade_intent()
        if _lexists(self._upgrade_intent_path()) and upgrade_intent is None:
            return {
                "action": "status",
                "assets_present": len(present),
                "status": "drifted",
            }
        if not _lexists(self.state_path):
            if upgrade_intent is not None:
                return {
                    "action": "status",
                    "assets_present": len(present),
                    "status": "drifted",
                }
            if intent is not None:
                return {
                    "action": "status",
                    "assets_present": len(present),
                    "status": "installing",
                }
            return {
                "action": "status",
                "assets_present": len(present),
                "status": "absent" if not present else "rollback_required",
            }
        try:
            state = self._validated_state_manifest()
            if upgrade_intent is not None:
                self._validate_upgrade_status(state, upgrade_intent)
                return {
                    "action": "status",
                    "assets_present": len(present),
                    "source_commit": state.commit,
                    "target_commit": str(upgrade_intent["commit"]),
                    "status": "upgrading",
                }
            self._require_state_destinations_exact(state)
        except (HostAssetInstallError, OSError):
            return {
                "action": "status",
                "assets_present": len(present),
                "status": "drifted",
            }
        return {
            "action": "status",
            "assets_present": len(present),
            "source_commit": state.commit,
            "status": "installed",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "plan/apply 经 sudo 读取非 root checkout 时，SUDO_UID 必须为与项目根 owner "
            "一致的十进制 UID；不会配置 safe.directory。"
            "status 仅在完整 installed 且无漂移时退出 0；absent、installing、"
            "rollback_required、incomplete、drifted 均退出 1。"
        ),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--expected-commit", required=True)
    plan.add_argument("--from-commit")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--expected-commit", required=True)
    apply.add_argument("--from-commit")
    apply.add_argument("--confirm-dedicated-production-host", action="store_true")
    apply.add_argument("--confirm-vcenter-storage-reviewed", action="store_true")
    resume = subparsers.add_parser("resume")
    resume.add_argument("--expected-commit", required=True)
    resume.add_argument("--from-commit")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--expected-commit", required=True)
    rollback.add_argument("--from-commit")
    rollback.add_argument("--confirm-rollback-this-install", action="store_true")
    accept = subparsers.add_parser("upgrade-accept")
    accept.add_argument("--expected-commit", required=True)
    accept.add_argument("--from-commit", required=True)
    subparsers.add_parser("status")
    return parser


def _result_exit_code(result: dict[str, object]) -> int:
    successful_results = {
        ("plan", "ready"),
        ("apply", "installed"),
        ("apply", "awaiting_acceptance"),
        ("resume", "installed"),
        ("resume", "awaiting_acceptance"),
        ("upgrade-accept", "installed"),
        ("rollback", "rolled_back"),
        ("status", "installed"),
    }
    return (
        0 if (result.get("action"), result.get("status")) in successful_results else 1
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    installer = ProductionHostAssetInstaller()
    try:
        if arguments.action == "plan":
            if arguments.from_commit is None:
                result = installer.plan(arguments.expected_commit)
            else:
                result = installer.plan(
                    arguments.expected_commit, from_commit=arguments.from_commit
                )
        elif arguments.action == "apply":
            result = installer.apply(
                arguments.expected_commit,
                confirm_dedicated_production_host=(
                    arguments.confirm_dedicated_production_host
                ),
                confirm_vcenter_storage_reviewed=(
                    arguments.confirm_vcenter_storage_reviewed
                ),
                from_commit=arguments.from_commit,
            )
        elif arguments.action == "resume":
            if arguments.from_commit is None:
                result = installer.resume(arguments.expected_commit)
            else:
                result = installer.resume(
                    arguments.expected_commit, from_commit=arguments.from_commit
                )
        elif arguments.action == "rollback":
            if arguments.from_commit is None:
                result = installer.rollback(
                    arguments.expected_commit,
                    confirm_rollback_this_install=(
                        arguments.confirm_rollback_this_install
                    ),
                )
            else:
                result = installer.rollback(
                    arguments.expected_commit,
                    confirm_rollback_this_install=(
                        arguments.confirm_rollback_this_install
                    ),
                    from_commit=arguments.from_commit,
                )
        elif arguments.action == "upgrade-accept":
            result = installer.upgrade_accept(
                arguments.expected_commit,
                from_commit=arguments.from_commit,
            )
        else:
            result = installer.status()
    except HostAssetInstallError:
        print(
            json.dumps(
                {"action": arguments.action, "status": "blocked"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return _result_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
