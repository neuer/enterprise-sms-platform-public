#!/usr/bin/env python3
"""规划、安装并只读核验固定的生产宿主资产。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SOURCE_ROOT = Path("/opt/sms-platform")
ETC_ROOT = Path("/etc/sms-platform")
SYSTEMD_ROOT = Path("/etc/systemd/system")
LOCAL_SBIN_ROOT = Path("/usr/local/sbin")
DOCKER_ROOT = Path("/var/lib/docker")
STATE_PATH = ETC_ROOT / "production-host-assets.json"
GIT_BINARY = "/usr/bin/git"
PYTHON_BINARY = "/usr/bin/python3"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
FINDMNT_BINARY = "/usr/bin/findmnt"
COMMAND_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
COMMAND_OUTPUT_LIMIT_RETURN_CODE = 125
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_INLINE_PREFLIGHT_BYTES = 128 * 1024
MAX_STATE_BYTES = 128 * 1024
MAX_UID = 2**32 - 2
STATE_SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DECIMAL_UID_RE = re.compile(r"(?:0|[1-9][0-9]*)")

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
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
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

    def _require_services_inactive(self) -> None:
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

    def _inspect_plan(self, expected_commit: str) -> tuple[AssetSnapshot, ...]:
        snapshots = self._source_snapshots(expected_commit)
        self._validate_plan_directories()
        self._require_empty_destinations()
        self._require_storage_ready(snapshots)
        self._require_services_inactive()
        self._require_empty_docker_root()
        return snapshots

    def plan(self, expected_commit: str) -> dict[str, object]:
        """Run only read paths and return the bounded installation plan."""

        self._inspect_plan(expected_commit)
        return {
            "action": "plan",
            "assets": len(self.assets),
            "source_commit": expected_commit,
            "status": "ready",
        }

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

    def apply(
        self,
        expected_commit: str,
        *,
        confirm_dedicated_production_host: bool,
        confirm_vcenter_storage_reviewed: bool,
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
        snapshots = self._inspect_plan(expected_commit)
        self._create_destination_directories()
        self._require_empty_destinations()

        staged: list[tuple[Path, AssetSnapshot]] = []
        state_temporary: Path | None = None
        try:
            for snapshot in snapshots:
                if snapshot.spec.kind == "regular":
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
            wrapper_snapshot = next(
                snapshot for snapshot in snapshots if snapshot.spec.kind == "symlink"
            )
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
            _commit_regular_no_replace(state_temporary, self.state_path)
            state_temporary = None
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
            "action": "apply",
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

    def status(self) -> dict[str, object]:
        """Inspect the commit-last state and every installed asset without mutation."""

        present = [asset for asset in self.assets if _lexists(asset.destination)]
        if not _lexists(self.state_path):
            return {
                "action": "status",
                "assets_present": len(present),
                "status": "absent" if not present else "incomplete",
            }
        try:
            state = self._read_state()
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
            by_name: dict[str, dict[str, object]] = {}
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
                metadata = spec.destination.lstat()
                if spec.kind == "regular":
                    payload, metadata = _read_regular_no_follow(
                        spec.destination, MAX_ASSET_BYTES
                    )
                    if (
                        hashlib.sha256(payload).hexdigest() != item_sha256
                        or metadata.st_uid != self.expected_uid
                        or metadata.st_gid != self.expected_gid
                        or stat.S_IMODE(metadata.st_mode) != spec.mode
                    ):
                        raise HostAssetInstallError("installed host asset has drifted")
                else:
                    _validate_installed_wrapper(
                        spec,
                        item_sha256,
                        expected_uid=self.expected_uid,
                        expected_gid=self.expected_gid,
                    )
        except (HostAssetInstallError, OSError):
            return {
                "action": "status",
                "assets_present": len(present),
                "status": "drifted",
            }
        return {
            "action": "status",
            "assets_present": len(present),
            "source_commit": commit,
            "status": "installed",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "plan/apply 经 sudo 读取非 root checkout 时，SUDO_UID 必须为与项目根 owner "
            "一致的十进制 UID；不会配置 safe.directory。"
            "status 仅在完整 installed 且无漂移时退出 0；absent、incomplete、drifted 均退出 1。"
        ),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--expected-commit", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--expected-commit", required=True)
    apply.add_argument("--confirm-dedicated-production-host", action="store_true")
    apply.add_argument("--confirm-vcenter-storage-reviewed", action="store_true")
    subparsers.add_parser("status")
    return parser


def _result_exit_code(result: dict[str, object]) -> int:
    successful_results = {
        ("plan", "ready"),
        ("apply", "installed"),
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
            result = installer.plan(arguments.expected_commit)
        elif arguments.action == "apply":
            result = installer.apply(
                arguments.expected_commit,
                confirm_dedicated_production_host=(
                    arguments.confirm_dedicated_production_host
                ),
                confirm_vcenter_storage_reviewed=(
                    arguments.confirm_vcenter_storage_reviewed
                ),
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
