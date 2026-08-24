#!/usr/bin/env python3
"""Read-only, fail-closed validation for the production storage layout."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

GIB = 1024**3
FSTAB_PATH = Path("/etc/fstab")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")
DOCKER_INTERNAL_VOLUME_ROOT = Path("/var/lib/docker/volumes")
UUID_SOURCE = re.compile(r"^UUID=[A-Fa-f0-9-]+$")
ALLOWED_FILESYSTEMS = frozenset({"ext4", "xfs"})
FILESYSTEM_METADATA_TOLERANCE_PERCENT = 2
PreflightMode = Literal["observe", "release", "startup"]
PREFLIGHT_MODES: tuple[PreflightMode, ...] = ("observe", "release", "startup")


@dataclass(frozen=True, slots=True)
class MountRequirement:
    name: str
    path: Path
    nominal_gib: int
    uid: int
    gid: int
    mode: int

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


MOUNT_REQUIREMENTS = (
    MountRequirement("os", Path("/"), 100, 0, 0, 0o755),
    MountRequirement("docker", Path("/var/lib/docker"), 250, 0, 0, 0o711),
    MountRequirement(
        "postgres", Path("/var/lib/sms-platform/postgres"), 400, 0, 0, 0o750
    ),
    MountRequirement("redis", Path("/var/lib/sms-platform/redis"), 100, 0, 0, 0o750),
    MountRequirement(
        "runtime", Path("/var/lib/sms-platform/runtime"), 200, 0, 0, 0o750
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

FORBIDDEN_DOCKER_INTERNAL_PATHS = tuple(
    candidate
    for name in (
        "sms-platform_pgdata",
        "sms-platform_redisdata",
        "sms-platform_redisauthdata",
        "sms-platform_rediscontroldata",
        "sms-platform_importdata",
        "sms-platform_exportdata",
        "sms-platform_rawspill",
    )
    for candidate in (
        DOCKER_INTERNAL_VOLUME_ROOT / name,
        DOCKER_INTERNAL_VOLUME_ROOT / name / "_data",
    )
)


@dataclass(frozen=True, slots=True)
class FstabEntry:
    source: str
    mount_path: Path
    fs_type: str
    options: frozenset[str]


@dataclass(frozen=True, slots=True)
class MountInfo:
    mount_path: Path
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
    def read_text(self, path: Path) -> str: ...

    def lstat(self, path: Path) -> PathStatus: ...

    def statvfs(self, path: Path) -> FilesystemStatus: ...

    def lexists(self, path: Path) -> bool: ...

    def resolve(self, path: Path) -> Path: ...

    def block_device_identity(self, uuid: str) -> tuple[int, int]: ...


class LocalProbe:
    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def lstat(self, path: Path) -> os.stat_result:
        return path.lstat()

    def statvfs(self, path: Path) -> os.statvfs_result:
        return os.statvfs(path)

    def lexists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def resolve(self, path: Path) -> Path:
        return path.resolve(strict=True)

    def block_device_identity(self, uuid: str) -> tuple[int, int]:
        resolved = Path("/dev/disk/by-uuid", uuid.casefold()).resolve(strict=True)
        status = resolved.stat()
        if not stat.S_ISBLK(status.st_mode):
            raise StorageContractError("UUID does not resolve to a block device")
        return os.major(status.st_rdev), os.minor(status.st_rdev)


class StorageContractError(ValueError):
    """The host storage declarations cannot be interpreted safely."""


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
        if len(fields) < 4:
            raise StorageContractError(f"invalid fstab entry at line {line_number}")
        entries.append(
            FstabEntry(
                source=_unescape_mount_field(fields[0]),
                mount_path=Path(_unescape_mount_field(fields[1])),
                fs_type=fields[2],
                options=frozenset(fields[3].split(",")),
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
    fstab = parse_fstab(active_probe.read_text(FSTAB_PATH))
    mountinfo = parse_mountinfo(active_probe.read_text(MOUNTINFO_PATH))
    findings: list[Finding] = []
    usages: list[Usage] = []

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

    for path in FORBIDDEN_DOCKER_INTERNAL_PATHS:
        if active_probe.lexists(path) and stat.S_ISLNK(active_probe.lstat(path).st_mode):
            findings.append(
                _finding(
                    "docker_internal_volume_symlink",
                    path,
                    "Docker internal volume directories must not be symlinks",
                )
            )

    mount_stats: dict[Path, PathStatus] = {}
    fstab_sources: dict[str, Path] = {}
    mounted_devices: dict[tuple[int, int], Path] = {}
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
            if fstab_entry.fs_type not in ALLOWED_FILESYSTEMS:
                findings.append(
                    _finding(
                        "fstab_filesystem_not_allowed",
                        mount_requirement.path,
                        f"filesystem type {fstab_entry.fs_type} is not approved",
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
            forbidden_options = {"nofail", "noauto", "x-systemd.automount"}
            present = sorted(fstab_entry.options & forbidden_options)
            if present:
                findings.append(
                    _finding(
                        "fstab_weak_dependency",
                        mount_requirement.path,
                        f"forbidden fstab option: {','.join(present)}",
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
            if mount_entry.fs_type not in ALLOWED_FILESYSTEMS:
                findings.append(
                    _finding(
                        "mounted_filesystem_not_allowed",
                        mount_requirement.path,
                        f"filesystem type {mount_entry.fs_type} is not approved",
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
            previous_path = mounted_devices.setdefault(device, mount_requirement.path)
            if previous_path != mount_requirement.path:
                findings.append(
                    _finding(
                        "shared_failure_domain",
                        mount_requirement.path,
                        f"filesystem device is already used by {previous_path}",
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
