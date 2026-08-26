#!/usr/bin/env python3
"""Read-only, fail-closed validation for the production storage layout."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

GIB = 1024**3
FSTAB_PATH = Path("/etc/fstab")
MOUNTINFO_PATH = Path("/proc/1/mountinfo")
PID1_COMM_PATH = Path("/proc/1/comm")
PID1_ROOT_PATH = Path("/proc/1/root")
PID1_MOUNT_NAMESPACE_PATH = Path("/proc/1/ns/mnt")
SYS_VENDOR_PATH = Path("/sys/class/dmi/id/sys_vendor")
SYSTEMD_DETECT_VIRT_BINARY = "/usr/bin/systemd-detect-virt"
FINDMNT_BINARY = "/usr/bin/findmnt"
DOCKER_MOUNT_PATH = Path("/var/lib/docker")
DOCKER_INTERNAL_VOLUME_ROOT = Path("/var/lib/docker/volumes")
UUID_SOURCE = re.compile(r"^UUID=[A-Fa-f0-9-]+$")
XFS_INFO_BINARY = "/usr/sbin/xfs_info"
MAX_XFS_INFO_BYTES = 64 * 1024
XFS_INFO_TIMEOUT_SECONDS = 5
HOST_PROBE_TIMEOUT_SECONDS = 3
FINDMNT_TIMEOUT_SECONDS = 5
MAX_FSTAB_BYTES = 1024 * 1024
MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024
FILESYSTEM_METADATA_TOLERANCE_PERCENT = 2
PreflightMode = Literal["observe", "release", "startup"]
PREFLIGHT_MODES: tuple[PreflightMode, ...] = ("observe", "release", "startup")
PSEUDO_FSTAB_TYPES = frozenset(
    {"cgroup", "cgroup2", "devpts", "proc", "securityfs", "sysfs", "tmpfs"}
)


@dataclass(frozen=True, slots=True)
class MountRequirement:
    name: str
    path: Path
    nominal_gib: int
    uid: int
    gid: int
    mode: int
    fs_type: Literal["ext4", "xfs"]

    @property
    def minimum_filesystem_bytes(self) -> int:
        return (
            self.nominal_gib
            * GIB
            * (100 - FILESYSTEM_METADATA_TOLERANCE_PERCENT)
            // 100
        )


@dataclass(frozen=True, slots=True)
class DirectoryRequirement:
    name: str
    path: Path
    mount_path: Path
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True, slots=True)
class DockerBindMountRequirement:
    name: str
    source_path: Path
    source_mount_path: Path
    target_path: Path

    @property
    def filesystem_root(self) -> Path:
        return Path("/") / self.source_path.relative_to(self.source_mount_path)


MOUNT_REQUIREMENTS = (
    MountRequirement("os", Path("/"), 100, 0, 0, 0o755, "ext4"),
    MountRequirement("docker", DOCKER_MOUNT_PATH, 250, 0, 0, 0o711, "xfs"),
    MountRequirement(
        "postgres", Path("/var/lib/sms-platform/postgres"), 400, 0, 0, 0o750, "xfs"
    ),
    MountRequirement(
        "redis", Path("/var/lib/sms-platform/redis"), 100, 0, 0, 0o750, "xfs"
    ),
    MountRequirement(
        "runtime", Path("/var/lib/sms-platform/runtime"), 200, 0, 0, 0o750, "xfs"
    ),
)

DIRECTORY_REQUIREMENTS = (
    DirectoryRequirement(
        "pgdata",
        Path("/var/lib/sms-platform/postgres/pgdata"),
        Path("/var/lib/sms-platform/postgres"),
        70,
        70,
        0o700,
    ),
    DirectoryRequirement(
        "redis-broker",
        Path("/var/lib/sms-platform/redis/broker"),
        Path("/var/lib/sms-platform/redis"),
        999,
        1000,
        0o700,
    ),
    DirectoryRequirement(
        "redis-auth",
        Path("/var/lib/sms-platform/redis/auth"),
        Path("/var/lib/sms-platform/redis"),
        999,
        1000,
        0o700,
    ),
    DirectoryRequirement(
        "redis-control",
        Path("/var/lib/sms-platform/redis/control"),
        Path("/var/lib/sms-platform/redis"),
        999,
        1000,
        0o700,
    ),
    DirectoryRequirement(
        "imports",
        Path("/var/lib/sms-platform/runtime/imports"),
        Path("/var/lib/sms-platform/runtime"),
        10001,
        10001,
        0o700,
    ),
    DirectoryRequirement(
        "exports",
        Path("/var/lib/sms-platform/runtime/exports"),
        Path("/var/lib/sms-platform/runtime"),
        10001,
        10001,
        0o700,
    ),
    DirectoryRequirement(
        "raw-spill",
        Path("/var/lib/sms-platform/runtime/raw-spill"),
        Path("/var/lib/sms-platform/runtime"),
        10001,
        10001,
        0o700,
    ),
    DirectoryRequirement(
        "backups",
        Path("/var/lib/sms-platform/runtime/backups"),
        Path("/var/lib/sms-platform/runtime"),
        0,
        0,
        0o700,
    ),
)

DOCKER_BIND_MOUNT_REQUIREMENTS = tuple(
    DockerBindMountRequirement(name, source_path, source_mount_path, target_path)
    for name, source_path, source_mount_path, target_path in (
        (
            "pgdata",
            Path("/var/lib/sms-platform/postgres/pgdata"),
            Path("/var/lib/sms-platform/postgres"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_pgdata" / "_data",
        ),
        (
            "redisdata",
            Path("/var/lib/sms-platform/redis/broker"),
            Path("/var/lib/sms-platform/redis"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_redisdata" / "_data",
        ),
        (
            "redisauthdata",
            Path("/var/lib/sms-platform/redis/auth"),
            Path("/var/lib/sms-platform/redis"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_redisauthdata" / "_data",
        ),
        (
            "rediscontroldata",
            Path("/var/lib/sms-platform/redis/control"),
            Path("/var/lib/sms-platform/redis"),
            DOCKER_INTERNAL_VOLUME_ROOT
            / "sms-platform_rediscontroldata"
            / "_data",
        ),
        (
            "importdata",
            Path("/var/lib/sms-platform/runtime/imports"),
            Path("/var/lib/sms-platform/runtime"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_importdata" / "_data",
        ),
        (
            "exportdata",
            Path("/var/lib/sms-platform/runtime/exports"),
            Path("/var/lib/sms-platform/runtime"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_exportdata" / "_data",
        ),
        (
            "rawspill",
            Path("/var/lib/sms-platform/runtime/raw-spill"),
            Path("/var/lib/sms-platform/runtime"),
            DOCKER_INTERNAL_VOLUME_ROOT / "sms-platform_rawspill" / "_data",
        ),
    )
)
DOCKER_CONTROL_DIRECTORIES = (
    DOCKER_INTERNAL_VOLUME_ROOT,
    *(requirement.target_path.parent for requirement in DOCKER_BIND_MOUNT_REQUIREMENTS),
)

FORBIDDEN_DOCKER_INTERNAL_PATHS = tuple(
    candidate
    for requirement in DOCKER_BIND_MOUNT_REQUIREMENTS
    for candidate in (
        requirement.target_path.parent,
        requirement.target_path,
    )
)


@dataclass(frozen=True, slots=True)
class FstabEntry:
    source: str
    mount_path: Path
    fs_type: str
    options: frozenset[str]
    dump: int
    pass_number: int


@dataclass(frozen=True, slots=True)
class MountInfo:
    mount_path: Path
    filesystem_root: Path
    major: int
    minor: int
    fs_type: str
    source: str
    options: frozenset[str]


@dataclass(frozen=True, slots=True)
class Finding:
    level: str
    code: str
    path: Path
    detail: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class Usage:
    name: str
    path: Path
    nominal_vmdk_gib: int
    minimum_filesystem_bytes: int
    capacity_bytes: int
    used_percent: float


@dataclass(frozen=True, slots=True)
class PreflightReport:
    findings: tuple[Finding, ...]
    usages: tuple[Usage, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.blocking for finding in self.findings)


class PathStatus(Protocol):
    @property
    def st_mode(self) -> int: ...

    @property
    def st_uid(self) -> int: ...

    @property
    def st_gid(self) -> int: ...

    @property
    def st_dev(self) -> int: ...


class FilesystemStatus(Protocol):
    @property
    def f_frsize(self) -> int: ...

    @property
    def f_bsize(self) -> int: ...

    @property
    def f_blocks(self) -> int: ...

    @property
    def f_bfree(self) -> int: ...

    @property
    def f_bavail(self) -> int: ...


class Probe(Protocol):
    def assert_host_context(self) -> None: ...

    def read_text(self, path: Path) -> str: ...

    def read_host_mountinfo(self) -> str: ...

    def lstat(self, path: Path) -> PathStatus: ...

    def statvfs(self, path: Path) -> FilesystemStatus: ...

    def resolve(self, path: Path) -> Path: ...

    def block_device_identity(self, uuid: str) -> tuple[int, int]: ...

    def xfs_ftype(self, path: Path) -> int: ...

    def verify_fstab(self, path: Path) -> None: ...

    def fstab_source_identity(self, entry: FstabEntry) -> tuple[int, int] | None: ...


class LocalProbe:
    def assert_host_context(self) -> None:
        if os.geteuid() != 0:
            raise StorageContractError("root privileges are required")
        try:
            host_root = Path("/").stat()
            pid1_root = PID1_ROOT_PATH.stat()
        except OSError as error:
            raise StorageContractError("host root identity unavailable") from error
        if (host_root.st_dev, host_root.st_ino) != (
            pid1_root.st_dev,
            pid1_root.st_ino,
        ):
            raise StorageContractError("chroot execution is forbidden")
        if _read_root_owned_text(PID1_COMM_PATH, maximum_bytes=256) != "systemd\n":
            raise StorageContractError("PID 1 is not systemd")
        container = self._detect_virtualization("--quiet", "--container")
        if container.returncode != 1 or container.stdout or container.stderr:
            raise StorageContractError("container execution is forbidden")
        vm = self._detect_virtualization("--vm")
        if (
            vm.returncode != 0
            or vm.stdout != b"vmware\n"
            or vm.stderr
        ):
            raise StorageContractError("VMware virtualization is required")
        if (
            _read_root_owned_text(SYS_VENDOR_PATH, maximum_bytes=256).strip()
            != "VMware, Inc."
        ):
            raise StorageContractError("VMware DMI identity is required")

    def read_text(self, path: Path) -> str:
        if path != FSTAB_PATH:
            raise StorageContractError("unexpected text path")
        return _read_root_owned_text(path, maximum_bytes=MAX_FSTAB_BYTES)

    def read_host_mountinfo(self) -> str:
        try:
            namespace_before = os.readlink(PID1_MOUNT_NAMESPACE_PATH)
            contents = _read_root_owned_text(
                MOUNTINFO_PATH,
                maximum_bytes=MAX_MOUNTINFO_BYTES,
            )
            namespace_after = os.readlink(PID1_MOUNT_NAMESPACE_PATH)
        except OSError as error:
            raise StorageContractError("host mount namespace unavailable") from error
        if (
            not namespace_before.startswith("mnt:[")
            or not namespace_before.endswith("]")
            or namespace_before != namespace_after
        ):
            raise StorageContractError("host mount namespace changed")
        return contents

    def _detect_virtualization(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                (SYSTEMD_DETECT_VIRT_BINARY, *arguments),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=HOST_PROBE_TIMEOUT_SECONDS,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StorageContractError("virtualization probe unavailable") from error

    def lstat(self, path: Path) -> os.stat_result:
        return path.lstat()

    def statvfs(self, path: Path) -> os.statvfs_result:
        return os.statvfs(path)

    def resolve(self, path: Path) -> Path:
        return path.resolve(strict=True)

    def block_device_identity(self, uuid: str) -> tuple[int, int]:
        resolved = Path("/dev/disk/by-uuid", uuid).resolve(strict=True)
        status = resolved.stat()
        if not stat.S_ISBLK(status.st_mode):
            raise StorageContractError("UUID does not resolve to a block device")
        return os.major(status.st_rdev), os.minor(status.st_rdev)

    def xfs_ftype(self, path: Path) -> int:
        try:
            completed = subprocess.run(
                (XFS_INFO_BINARY, str(path)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=XFS_INFO_TIMEOUT_SECONDS,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StorageContractError("xfs_info unavailable") from error
        if (
            completed.returncode != 0
            or completed.stderr
            or len(completed.stdout) > MAX_XFS_INFO_BYTES
        ):
            raise StorageContractError("xfs_info failed")
        values = re.findall(
            rb"(?:^|[,\s])ftype=([01])(?=$|[,\s])",
            completed.stdout,
        )
        if len(values) != 1:
            raise StorageContractError("xfs_info output invalid")
        return int(values[0])

    def verify_fstab(self, path: Path) -> None:
        try:
            completed = subprocess.run(
                (FINDMNT_BINARY, "--verify", "--tab-file", str(path)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=FINDMNT_TIMEOUT_SECONDS,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StorageContractError("findmnt verify unavailable") from error
        if (
            completed.returncode != 0
            or completed.stdout.strip()
            != b"Success, no errors or warnings detected"
            or completed.stderr
        ):
            raise StorageContractError("findmnt verify failed")

    def fstab_source_identity(self, entry: FstabEntry) -> tuple[int, int] | None:
        path = _fstab_source_path(entry)
        if path is None:
            return None
        resolved = path.resolve(strict=True)
        status = resolved.stat()
        if stat.S_ISBLK(status.st_mode):
            return os.major(status.st_rdev), os.minor(status.st_rdev)
        if stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode):
            if not ({"bind", "rbind"} & entry.options):
                raise StorageContractError("non-block fstab source is not a bind")
            return os.major(status.st_dev), os.minor(status.st_dev)
        raise StorageContractError("fstab source identity unavailable")


class StorageContractError(ValueError):
    """The host storage declarations cannot be interpreted safely."""


def _read_root_owned_text(path: Path, *, maximum_bytes: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise StorageContractError("root-owned text metadata is unsafe")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except StorageContractError:
        raise
    except OSError as error:
        raise StorageContractError("root-owned text is unavailable") from error
    payload = b"".join(chunks)
    if not payload or len(payload) > maximum_bytes or b"\0" in payload:
        raise StorageContractError("root-owned text content is invalid")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise StorageContractError("root-owned text encoding is invalid") from error


def _fstab_source_path(entry: FstabEntry) -> Path | None:
    source = entry.source
    tag_directories = {
        "UUID": "by-uuid",
        "LABEL": "by-label",
        "PARTUUID": "by-partuuid",
        "PARTLABEL": "by-partlabel",
    }
    if "=" in source:
        tag, value = source.split("=", 1)
        directory = tag_directories.get(tag)
        if directory is None or not value or "/" in value or "\x00" in value:
            raise StorageContractError("unsupported fstab source tag")
        return Path("/dev/disk") / directory / value
    if source.startswith("/"):
        return Path(source)
    if entry.fs_type in PSEUDO_FSTAB_TYPES:
        return None
    raise StorageContractError("unsupported fstab source")


def _unescape_mount_field(value: str) -> str:
    escaped_values = (
        (r"\040", " "),
        (r"\011", "\t"),
        (r"\012", "\n"),
        (r"\134", "\\"),
    )
    for encoded, decoded in escaped_values:
        value = value.replace(encoded, decoded)
    return value


def parse_fstab(contents: str) -> tuple[FstabEntry, ...]:
    entries: list[FstabEntry] = []
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 6:
            raise StorageContractError(f"invalid fstab entry at line {line_number}")
        try:
            dump = int(fields[4])
            pass_number = int(fields[5])
        except ValueError as error:
            raise StorageContractError(
                f"invalid fstab entry at line {line_number}"
            ) from error
        if str(dump) != fields[4] or str(pass_number) != fields[5]:
            raise StorageContractError(f"invalid fstab entry at line {line_number}")
        entries.append(
            FstabEntry(
                source=_unescape_mount_field(fields[0]),
                mount_path=Path(_unescape_mount_field(fields[1])),
                fs_type=fields[2],
                options=frozenset(fields[3].split(",")),
                dump=dump,
                pass_number=pass_number,
            )
        )
    return tuple(entries)


def parse_mountinfo(contents: str) -> tuple[MountInfo, ...]:
    entries: list[MountInfo] = []
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        fields = raw_line.split()
        try:
            separator = fields.index("-")
            raw_major, raw_minor = fields[2].split(":", maxsplit=1)
            entries.append(
                MountInfo(
                    mount_path=Path(_unescape_mount_field(fields[4])),
                    filesystem_root=Path(_unescape_mount_field(fields[3])),
                    major=int(raw_major),
                    minor=int(raw_minor),
                    fs_type=fields[separator + 1],
                    source=_unescape_mount_field(fields[separator + 2]),
                    options=frozenset(fields[5].split(",")),
                )
            )
        except (IndexError, ValueError) as error:
            raise StorageContractError(
                f"invalid mountinfo entry at line {line_number}"
            ) from error
    return tuple(entries)


def _single_entry_by_path[T](entries: tuple[T, ...], path: Path) -> T | None:
    matching = tuple(entry for entry in entries if entry.mount_path == path)  # type: ignore[attr-defined]
    if len(matching) > 1:
        raise StorageContractError(f"duplicate declaration for {path}")
    return matching[0] if matching else None


def _finding(
    code: str,
    path: Path,
    detail: str,
    *,
    level: str = "error",
    blocking: bool = True,
) -> Finding:
    return Finding(level, code, path, detail, blocking)


def _permission_findings(
    path: Path,
    status: PathStatus,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> list[Finding]:
    findings: list[Finding] = []
    if stat.S_ISLNK(status.st_mode):
        return [_finding("symlink_forbidden", path, "required path must not be a symlink")]
    if not stat.S_ISDIR(status.st_mode):
        return [_finding("not_directory", path, "required path is not a directory")]
    actual_mode = stat.S_IMODE(status.st_mode)
    if status.st_uid != uid or status.st_gid != gid:
        findings.append(
            _finding(
                "wrong_owner",
                path,
                f"expected uid:gid {uid}:{gid}, got {status.st_uid}:{status.st_gid}",
            )
        )
    if actual_mode != mode:
        findings.append(
            _finding(
                "wrong_mode",
                path,
                f"expected {mode:04o}, got {actual_mode:04o}",
            )
        )
    return findings


def _usage_for(
    requirement: MountRequirement, filesystem: FilesystemStatus
) -> Usage:
    fragment_size = filesystem.f_frsize or filesystem.f_bsize
    capacity_bytes = filesystem.f_blocks * fragment_size
    used_blocks = filesystem.f_blocks - filesystem.f_bfree
    accountable_blocks = used_blocks + filesystem.f_bavail
    if fragment_size <= 0 or filesystem.f_blocks <= 0 or accountable_blocks <= 0:
        raise StorageContractError(
            f"invalid filesystem accounting for {requirement.path}"
        )
    return Usage(
        requirement.name,
        requirement.path,
        requirement.nominal_gib,
        requirement.minimum_filesystem_bytes,
        capacity_bytes,
        used_blocks * 100 / accountable_blocks,
    )


def _usage_finding(usage: Usage, mode: PreflightMode) -> Finding | None:
    detail = f"filesystem utilization is {usage.used_percent:.1f}%"
    if usage.used_percent >= 90:
        return _finding(
            "usage_emergency",
            usage.path,
            detail,
            level="emergency",
            blocking=mode in {"release", "startup"},
        )
    if usage.used_percent >= 80:
        return _finding(
            "usage_critical",
            usage.path,
            detail,
            level="critical",
            blocking=mode == "release",
        )
    if usage.used_percent >= 70:
        return _finding("usage_warning", usage.path, detail, level="warning", blocking=False)
    return None


def inspect_storage(
    probe: Probe | None = None,
    *,
    mode: PreflightMode = "startup",
) -> PreflightReport:
    """Inspect declarations and mounted filesystems without changing host state."""

    if mode not in PREFLIGHT_MODES:
        raise StorageContractError("invalid preflight mode")
    active_probe: Probe = probe if probe is not None else LocalProbe()
    active_probe.assert_host_context()
    fstab_text = active_probe.read_text(FSTAB_PATH)
    fstab = parse_fstab(fstab_text)
    mountinfo = parse_mountinfo(active_probe.read_host_mountinfo())
    findings: list[Finding] = []
    usages: list[Usage] = []
    try:
        active_probe.verify_fstab(FSTAB_PATH)
    except (OSError, StorageContractError) as error:
        findings.append(
            _finding("fstab_verification_failed", FSTAB_PATH, type(error).__name__)
        )

    for entry in fstab:
        source_path = Path(entry.source) if entry.source.startswith("/") else None
        target_is_internal = entry.mount_path == DOCKER_INTERNAL_VOLUME_ROOT or (
            DOCKER_INTERNAL_VOLUME_ROOT in entry.mount_path.parents
        )
        source_is_internal = source_path is not None and (
            source_path == DOCKER_INTERNAL_VOLUME_ROOT
            or DOCKER_INTERNAL_VOLUME_ROOT in source_path.parents
        )
        if target_is_internal or source_is_internal:
            findings.append(
                _finding(
                    "docker_internal_volume_fstab",
                    entry.mount_path,
                    "fstab must not mount, bind, or relocate Docker internal volume paths",
                )
            )

    docker_control_stats: dict[Path, PathStatus] = {}
    for path in DOCKER_CONTROL_DIRECTORIES:
        try:
            metadata = active_probe.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            findings.append(
                _finding(
                    "docker_volume_control_path_unsafe",
                    path,
                    type(error).__name__,
                )
            )
            continue
        try:
            canonical = active_probe.resolve(path)
        except OSError as error:
            findings.append(
                _finding(
                    "docker_volume_control_path_unsafe",
                    path,
                    type(error).__name__,
                )
            )
            continue
        docker_control_stats[path] = metadata
        if (
            canonical != path
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or (metadata.st_mode & stat.S_IXUSR) == 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            findings.append(
                _finding(
                    "docker_volume_control_path_unsafe",
                    path,
                    "Docker volume control directory is not canonical and root-controlled",
                )
            )

    for path in FORBIDDEN_DOCKER_INTERNAL_PATHS:
        try:
            metadata = active_probe.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            findings.append(
                _finding(
                    "docker_internal_volume_path_unavailable",
                    path,
                    type(error).__name__,
                )
            )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            findings.append(
                _finding(
                    "docker_internal_volume_symlink",
                    path,
                    "Docker internal volume directories must not be symlinks",
                )
            )

    mount_stats: dict[Path, PathStatus] = {}
    fstab_sources: dict[str, Path] = {}
    managed_source_devices: dict[tuple[int, int], Path] = {}
    mounted_devices: dict[tuple[int, int], Path] = {}
    mounted_devices_by_path: dict[Path, tuple[int, int]] = {}
    for mount_requirement in MOUNT_REQUIREMENTS:
        fstab_entry = _single_entry_by_path(fstab, mount_requirement.path)
        fstab_device: tuple[int, int] | None = None
        if fstab_entry is None:
            findings.append(
                _finding(
                    "fstab_missing",
                    mount_requirement.path,
                    "required fstab entry is missing",
                )
            )
        else:
            if UUID_SOURCE.fullmatch(fstab_entry.source) is None:
                findings.append(
                    _finding(
                        "fstab_not_uuid",
                        mount_requirement.path,
                        "block filesystem source must use UUID=",
                    )
                )
            else:
                raw_uuid = fstab_entry.source.removeprefix("UUID=")
                try:
                    fstab_device = active_probe.block_device_identity(raw_uuid)
                except (OSError, StorageContractError) as error:
                    findings.append(
                        _finding(
                            "fstab_uuid_unresolved",
                            mount_requirement.path,
                            type(error).__name__,
                        )
                    )
                else:
                    previous_device_path = managed_source_devices.setdefault(
                        fstab_device,
                        mount_requirement.path,
                    )
                    if previous_device_path != mount_requirement.path:
                        findings.append(
                            _finding(
                                "fstab_device_reused",
                                mount_requirement.path,
                                (
                                    "resolved fstab device is already assigned to "
                                    f"{previous_device_path}"
                                ),
                            )
                        )
            if fstab_entry.fs_type != mount_requirement.fs_type:
                findings.append(
                    _finding(
                        "fstab_filesystem_mismatch",
                        mount_requirement.path,
                        (
                            f"expected {mount_requirement.fs_type}, "
                            f"got {fstab_entry.fs_type}"
                        ),
                    )
                )
            normalized_source = fstab_entry.source.casefold()
            previous_path = fstab_sources.setdefault(
                normalized_source, mount_requirement.path
            )
            if previous_path != mount_requirement.path:
                findings.append(
                    _finding(
                        "fstab_uuid_reused",
                        mount_requirement.path,
                        f"filesystem source is already assigned to {previous_path}",
                    )
                )
            forbidden_options = {"noauto", "nofail", "ro", "x-systemd.automount"}
            present = sorted(fstab_entry.options & forbidden_options)
            if present:
                findings.append(
                    _finding(
                        "fstab_weak_dependency",
                        mount_requirement.path,
                        f"forbidden fstab option: {','.join(present)}",
                    )
                )
            expected_pass_number = 1 if mount_requirement.path == Path("/") else 2
            if fstab_entry.dump != 0 or fstab_entry.pass_number != expected_pass_number:
                findings.append(
                    _finding(
                        "fstab_check_order_mismatch",
                        mount_requirement.path,
                        (
                            f"expected dump/pass 0 {expected_pass_number}, got "
                            f"{fstab_entry.dump} {fstab_entry.pass_number}"
                        ),
                    )
                )
            if mount_requirement.path != Path("/"):
                missing_options = sorted(
                    {"nodev", "nosuid"} - fstab_entry.options
                )
                if missing_options:
                    findings.append(
                        _finding(
                            "fstab_safety_options_missing",
                            mount_requirement.path,
                            f"required fstab option missing: {','.join(missing_options)}",
                        )
                    )

        mount_entry = _single_entry_by_path(mountinfo, mount_requirement.path)
        if mount_entry is None:
            findings.append(
                _finding(
                    "mount_missing",
                    mount_requirement.path,
                    "required filesystem is not mounted",
                )
            )
        else:
            if mount_entry.filesystem_root != Path("/"):
                findings.append(
                    _finding(
                        "mount_filesystem_root_mismatch",
                        mount_requirement.path,
                        (
                            "fixed block mount must expose filesystem root; got "
                            f"{mount_entry.filesystem_root}"
                        ),
                    )
                )
            if mount_entry.fs_type != mount_requirement.fs_type:
                findings.append(
                    _finding(
                        "mounted_filesystem_mismatch",
                        mount_requirement.path,
                        (
                            f"expected {mount_requirement.fs_type}, "
                            f"got {mount_entry.fs_type}"
                        ),
                    )
                )
            if "rw" not in mount_entry.options:
                findings.append(
                    _finding(
                        "mount_read_only",
                        mount_requirement.path,
                        "filesystem is not writable",
                    )
                )
            if mount_requirement.path != Path("/"):
                missing_options = sorted({"nodev", "nosuid"} - mount_entry.options)
                if missing_options:
                    findings.append(
                        _finding(
                            "mount_safety_options_missing",
                            mount_requirement.path,
                            f"required mount option missing: {','.join(missing_options)}",
                        )
                    )
            if fstab_device is not None and fstab_device != (
                mount_entry.major,
                mount_entry.minor,
            ):
                findings.append(
                    _finding(
                        "fstab_mount_identity_mismatch",
                        mount_requirement.path,
                        "fstab UUID device does not match the mounted filesystem",
                    )
                )
            device = (mount_entry.major, mount_entry.minor)
            mounted_devices_by_path[mount_requirement.path] = device
            previous_path = mounted_devices.setdefault(device, mount_requirement.path)
            if previous_path != mount_requirement.path:
                findings.append(
                    _finding(
                        "shared_failure_domain",
                        mount_requirement.path,
                        f"filesystem device is already used by {previous_path}",
                    )
                )

            if mount_requirement.fs_type == "xfs" and mount_entry.fs_type == "xfs":
                try:
                    ftype = active_probe.xfs_ftype(mount_requirement.path)
                except (OSError, StorageContractError) as error:
                    findings.append(
                        _finding(
                            "xfs_info_failed",
                            mount_requirement.path,
                            type(error).__name__,
                        )
                    )
                else:
                    if ftype != 1:
                        findings.append(
                            _finding(
                                "xfs_ftype_required",
                                mount_requirement.path,
                                f"expected ftype=1, got ftype={ftype}",
                            )
                        )

        try:
            if active_probe.resolve(mount_requirement.path) != mount_requirement.path:
                findings.append(
                    _finding(
                        "path_not_canonical",
                        mount_requirement.path,
                        "required path or one of its ancestors is a symlink",
                    )
                )
            status = active_probe.lstat(mount_requirement.path)
            mount_stats[mount_requirement.path] = status
            findings.extend(
                _permission_findings(
                    mount_requirement.path,
                    status,
                    uid=mount_requirement.uid,
                    gid=mount_requirement.gid,
                    mode=mount_requirement.mode,
                )
            )
            if mount_entry is not None and stat.S_ISDIR(status.st_mode):
                actual_device = (os.major(status.st_dev), os.minor(status.st_dev))
                if actual_device != (mount_entry.major, mount_entry.minor):
                    findings.append(
                        _finding(
                            "mount_identity_mismatch",
                            mount_requirement.path,
                            "path device does not match mountinfo",
                        )
                    )
        except OSError as error:
            findings.append(
                _finding(
                    "path_unavailable", mount_requirement.path, type(error).__name__
                )
            )
            continue

        try:
            usage = _usage_for(
                mount_requirement,
                active_probe.statvfs(mount_requirement.path),
            )
            usages.append(usage)
            if usage.capacity_bytes < mount_requirement.minimum_filesystem_bytes:
                findings.append(
                    _finding(
                        "filesystem_too_small",
                        mount_requirement.path,
                        (
                            f"nominal VMDK is {mount_requirement.nominal_gib} GiB; "
                            "after the "
                            f"{FILESYSTEM_METADATA_TOLERANCE_PERCENT}% filesystem "
                            "metadata tolerance, requires "
                            f"at least {mount_requirement.minimum_filesystem_bytes / GIB:.1f} GiB; "
                            f"got {usage.capacity_bytes / GIB:.1f} GiB"
                        ),
                    )
                )
            usage_finding = _usage_finding(usage, mode)
            if usage_finding is not None:
                findings.append(usage_finding)
        except (OSError, StorageContractError) as error:
            findings.append(
                _finding(
                    "filesystem_accounting_failed",
                    mount_requirement.path,
                    type(error).__name__,
                )
            )

    docker_mount_status = mount_stats.get(DOCKER_MOUNT_PATH)
    for path, status in docker_control_stats.items():
        if (
            docker_mount_status is not None
            and status.st_dev != docker_mount_status.st_dev
        ):
            findings.append(
                _finding(
                    "docker_volume_control_device_mismatch",
                    path,
                    "Docker volume control directory must remain on the Docker VMDK",
                )
            )
        if any(entry.mount_path == path for entry in mountinfo):
            findings.append(
                _finding(
                    "docker_volume_control_nested_mount",
                    path,
                    (
                        "Docker volume control directory must not be an independent "
                        "mount; only the seven fixed _data bind targets are allowed"
                    ),
                )
            )

    for fstab_alias_entry in fstab:
        try:
            source_device = active_probe.fstab_source_identity(fstab_alias_entry)
        except (OSError, StorageContractError) as error:
            findings.append(
                _finding(
                    "fstab_source_unresolved",
                    fstab_alias_entry.mount_path,
                    type(error).__name__,
                )
            )
            continue
        managed_path = (
            managed_source_devices.get(source_device)
            if source_device is not None
            else None
        )
        if managed_path is not None and fstab_alias_entry.mount_path != managed_path:
            findings.append(
                _finding(
                    "fstab_managed_source_alias",
                    fstab_alias_entry.mount_path,
                    f"managed source is already assigned to {managed_path}",
                )
            )

    approved_binds = {
        requirement.target_path: requirement
        for requirement in DOCKER_BIND_MOUNT_REQUIREMENTS
    }
    approved_bind_targets_seen: set[Path] = set()
    mount_requirements_by_path = {
        requirement.path: requirement for requirement in MOUNT_REQUIREMENTS
    }
    for mount_alias_entry in mountinfo:
        approved_bind = approved_binds.get(mount_alias_entry.mount_path)
        if approved_bind is not None:
            source_mount = mount_requirements_by_path.get(
                approved_bind.source_mount_path
            )
            expected_device = mounted_devices_by_path.get(
                approved_bind.source_mount_path
            )
            try:
                target_is_canonical_directory = (
                    active_probe.resolve(approved_bind.target_path)
                    == approved_bind.target_path
                    and stat.S_ISDIR(
                        active_probe.lstat(approved_bind.target_path).st_mode
                    )
                )
            except OSError:
                target_is_canonical_directory = False
            if (
                source_mount is not None
                and expected_device is not None
                and approved_bind.target_path not in approved_bind_targets_seen
                and expected_device
                == (mount_alias_entry.major, mount_alias_entry.minor)
                and approved_bind.filesystem_root
                == mount_alias_entry.filesystem_root
                and mount_alias_entry.fs_type == source_mount.fs_type
                and {"rw", "nodev", "nosuid"} <= mount_alias_entry.options
                and target_is_canonical_directory
            ):
                approved_bind_targets_seen.add(approved_bind.target_path)
                continue
            findings.append(
                _finding(
                    "docker_bind_mount_contract_mismatch",
                    mount_alias_entry.mount_path,
                    "Docker local-driver bind mount does not match its fixed contract",
                )
            )
            continue
        managed_path = mounted_devices.get(
            (mount_alias_entry.major, mount_alias_entry.minor)
        )
        if managed_path is not None and mount_alias_entry.mount_path != managed_path:
            findings.append(
                _finding(
                    "managed_device_mount_alias",
                    mount_alias_entry.mount_path,
                    f"managed device is already mounted at {managed_path}",
                )
            )

    for directory_requirement in DIRECTORY_REQUIREMENTS:
        if _single_entry_by_path(mountinfo, directory_requirement.path) is not None:
            findings.append(
                _finding(
                    "unexpected_nested_mount",
                    directory_requirement.path,
                    "fixed data subpath must be a directory on its declared VMDK",
                )
            )
        try:
            if (
                active_probe.resolve(directory_requirement.path)
                != directory_requirement.path
            ):
                findings.append(
                    _finding(
                        "path_not_canonical",
                        directory_requirement.path,
                        "required path or one of its ancestors is a symlink",
                    )
                )
            status = active_probe.lstat(directory_requirement.path)
        except OSError as error:
            findings.append(
                _finding(
                    "path_unavailable",
                    directory_requirement.path,
                    type(error).__name__,
                )
            )
            continue
        findings.extend(
            _permission_findings(
                directory_requirement.path,
                status,
                uid=directory_requirement.uid,
                gid=directory_requirement.gid,
                mode=directory_requirement.mode,
            )
        )
        mount_status = mount_stats.get(directory_requirement.mount_path)
        if mount_status is not None and status.st_dev != mount_status.st_dev:
            findings.append(
                _finding(
                    "subpath_wrong_filesystem",
                    directory_requirement.path,
                    "fixed data subpath is not on its declared VMDK",
                )
            )

    return PreflightReport(tuple(findings), tuple(usages))


def _emit(report: PreflightReport, mode: PreflightMode) -> None:
    for usage in report.usages:
        print(
            json.dumps(
                {
                    "event": "storage_preflight_usage",
                    "mount": usage.name,
                    "path": str(usage.path),
                    "nominal_vmdk_gib": usage.nominal_vmdk_gib,
                    "minimum_filesystem_gib": round(
                        usage.minimum_filesystem_bytes / GIB, 1
                    ),
                    "filesystem_capacity_gib": round(usage.capacity_bytes / GIB, 1),
                    "used_percent": round(usage.used_percent, 1),
                },
                sort_keys=True,
            )
        )
    for finding in report.findings:
        stream = sys.stderr if finding.level != "warning" else sys.stdout
        print(
            json.dumps(
                {
                    "event": "storage_preflight_finding",
                    "level": finding.level,
                    "code": finding.code,
                    "path": str(finding.path),
                    "detail": finding.detail,
                    "blocking": finding.blocking,
                },
                sort_keys=True,
            ),
            file=stream,
        )
    print(
        json.dumps(
            {
                "event": "storage_preflight_result",
                "mode": mode,
                "status": "passed" if report.passed else "failed",
                "findings": len(report.findings),
            },
            sort_keys=True,
        ),
        file=sys.stdout if report.passed else sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=PREFLIGHT_MODES,
        default="startup",
        help="startup blocks at 90%%; release blocks at 80%%; observe emits only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    mode: PreflightMode = arguments.mode
    try:
        report = inspect_storage(mode=mode)
    except (OSError, StorageContractError, UnicodeError) as error:
        print(
            json.dumps(
                {
                    "event": "storage_preflight_result",
                    "mode": mode,
                    "status": "failed",
                    "code": "inspection_unavailable",
                    "detail": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    _emit(report, mode)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
