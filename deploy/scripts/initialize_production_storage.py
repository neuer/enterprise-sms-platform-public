#!/usr/bin/python3 -I
"""Fail-closed, operator-driven provisioning of the production data VMDKs.

This is a one-time production-host tool.  It is deliberately not imported by the
application, Compose wrapper, release manager, or server-update workflow.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TextIO, cast

GIB = 1024**3
SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
INTENT_SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 30
MKFS_TIMEOUT_SECONDS = 300
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_CONTROL_FILE_BYTES = 512 * 1024
MAX_FSTAB_BYTES = 1024 * 1024
SAMPLE_BYTES = 1024 * 1024
SAMPLE_COUNT = 10
BLKGETSIZE64 = 0x80081272
AT_FDCWD = -100
RENAME_NOREPLACE = 1

MANIFEST_DEFAULT = Path("/etc/sms-platform/production-storage-manifest.json")
STATE_PATH = Path("/etc/sms-platform/production-storage-state.json")
INTENT_PATH = Path("/etc/sms-platform/production-storage-intent.json")
LOCK_PATH = Path("/etc/sms-platform/.production-storage-init.lock")
FSTAB_PATH = Path("/etc/fstab")
SMS_STORAGE_ROOT = Path("/var/lib/sms-platform")
MACHINE_ID_PATH = Path("/etc/machine-id")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PID1_MOUNT_NS = Path("/proc/1/ns/mnt")
SELF_MOUNT_NS = Path("/proc/self/ns/mnt")
HOST_ROOT_PATH = Path("/")
PID1_ROOT_PATH = Path("/proc/1/root")
PID1_COMM_PATH = Path("/proc/1/comm")
SYS_VENDOR_PATH = Path("/sys/class/dmi/id/sys_vendor")
PRODUCT_UUID_PATH = Path("/sys/class/dmi/id/product_uuid")
OS_RELEASE_PATH = Path("/usr/lib/os-release")
PROC_SWAPS_PATH = Path("/proc/swaps")
SWAP_FILE_PATH = Path("/swap.img")

LSBLK_BINARY = "/usr/bin/lsblk"
FINDMNT_BINARY = "/usr/bin/findmnt"
BLKID_BINARY = "/usr/sbin/blkid"
WIPEFS_BINARY = "/usr/sbin/wipefs"
MKFS_XFS_BINARY = "/usr/sbin/mkfs.xfs"
XFS_INFO_BINARY = "/usr/sbin/xfs_info"
MOUNT_BINARY = "/usr/bin/mount"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
SYSTEMD_DETECT_VIRT_BINARY = "/usr/bin/systemd-detect-virt"
UDEVADM_BINARY = "/usr/bin/udevadm"
TIMEDATECTL_BINARY = "/usr/bin/timedatectl"

SAFE_CHANGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
SAFE_REVIEWER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{1,63}")
SAFE_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+ /-]{0,127}")
MAJOR_MINOR_RE = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)")
BY_ID_BASENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{2,255}")
DM_LVM_BY_ID_BASENAME_RE = re.compile(r"dm-uuid-LVM-[A-Za-z0-9]{64}")
CANONICAL_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
FAT_UUID_RE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}")
XFS_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
LVM_IDENTIFIER_RE = re.compile(
    r"[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}"
)
SAFE_FSTAB_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
EFI_SYSTEM_PARTITION_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_FILESYSTEM_PARTITION_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"
OS_LOGICAL_SECTOR_BYTES = 512
OS_EFI_PARTITION_NUMBER = 1
OS_EFI_PARTITION_START_SECTOR = 2_048
OS_EFI_PARTITION_BYTES = 1_127_219_200
OS_BOOT_PARTITION_NUMBER = 2
OS_BOOT_PARTITION_START_SECTOR = 2_203_648
OS_BOOT_PARTITION_BYTES = 2 * GIB
OS_LVM_PARTITION_NUMBER = 3
OS_LVM_PARTITION_START_SECTOR = 6_397_952
OS_LVM_PARTITION_BASELINE_BYTES = 104_097_382_400
OS_ROOT_LV_BASELINE_BYTES = 104_094_236_672
OS_LAYOUT_TAIL_TOLERANCE_BYTES = 8 * 1024**2
OS_ROOT_FILESYSTEM_MINIMUM_BYTES = 94 * GIB
OS_ROOT_FILESYSTEM_MINIMUM_LV_PERCENT = 97
OS_ROOT_LV_PATH = Path("/dev/mapper/ubuntu--vg-ubuntu--lv")
SWAP_FILE_BYTES = 8 * GIB
SWAP_ACTIVE_HEADER_MAX_BYTES = 64 * 1024
EXPECTED_FINDMNT_SWAP_WARNING_STDOUT = (
    b"none\n"
    b"   [W] non-bind mount source /swap.img is a directory or regular file\n"
)
EXPECTED_FINDMNT_SWAP_WARNING_STDERR = (
    b"\n0 parse errors, 0 errors, 1 warning\n"
)
EXPECTED_FINDMNT_CLEAN_SUMMARY = b"Success, no errors or warnings detected"
PSEUDO_FSTAB_TYPES = frozenset(
    {
        "cgroup",
        "cgroup2",
        "devpts",
        "proc",
        "securityfs",
        "sysfs",
        "tmpfs",
    }
)

Role = Literal["os", "docker", "postgres", "redis", "runtime"]
DataRole = Literal["docker", "postgres", "redis", "runtime"]
Phase = Literal[
    "prepared",
    "formatting",
    "formatted",
    "fstab_prepared",
    "fstab_written",
    "mounting",
    "directories",
    "verifying",
]


class StorageInitializationError(RuntimeError):
    """A bounded production-storage failure with a safe machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _findmnt_verify_output_is_accepted(stdout: bytes, stderr: bytes) -> bool:
    """Accept a clean result or Noble's sole known swapfile warning."""

    clean = not stderr and stdout.strip() in {b"", EXPECTED_FINDMNT_CLEAN_SUMMARY}
    known_swap_warning = (
        stdout == EXPECTED_FINDMNT_SWAP_WARNING_STDOUT
        and stderr == EXPECTED_FINDMNT_SWAP_WARNING_STDERR
    )
    return clean or known_swap_warning


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        pass_fds: Sequence[int] = (),
    ) -> CommandResult: ...


class SubprocessRunner:
    """Run fixed absolute argv without inheriting operator-controlled settings."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        pass_fds: Sequence[int] = (),
    ) -> CommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            return CommandResult(126)
        inherited_fds = tuple(pass_fds)
        if (
            any(not isinstance(descriptor, int) or descriptor < 0 for descriptor in inherited_fds)
            or len(set(inherited_fds)) != len(inherited_fds)
        ):
            return CommandResult(126)
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
                pass_fds=inherited_fds,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "PYTHONNOUSERSITE": "1",
                },
            )
        except FileNotFoundError:
            return CommandResult(127)
        except subprocess.TimeoutExpired:
            return CommandResult(124)
        except OSError:
            return CommandResult(126)
        if (
            len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stdout) + len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            return CommandResult(125)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class DirectorySpec:
    path: Path
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role: Role
    nominal_gib: int
    mount_path: Path
    mount_uid: int
    mount_gid: int
    mount_mode: int
    label: str | None
    directories: tuple[DirectorySpec, ...] = ()

    @property
    def nominal_bytes(self) -> int:
        return self.nominal_gib * GIB


@dataclass(frozen=True, slots=True)
class DockerBindMountSpec:
    role: DataRole
    source_path: Path
    target_path: Path

    @property
    def filesystem_root(self) -> Path:
        return Path("/") / self.source_path.relative_to(
            SPECS_BY_ROLE[self.role].mount_path
        )


ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec("os", 100, Path("/"), 0, 0, 0o755, None),
    RoleSpec("docker", 250, Path("/var/lib/docker"), 0, 0, 0o710, "sms_docker"),
    RoleSpec(
        "postgres",
        400,
        Path("/var/lib/sms-platform/postgres"),
        0,
        0,
        0o750,
        "sms_pg",
        (DirectorySpec(Path("/var/lib/sms-platform/postgres/pgdata"), 70, 70, 0o700),),
    ),
    RoleSpec(
        "redis",
        100,
        Path("/var/lib/sms-platform/redis"),
        0,
        0,
        0o750,
        "sms_redis",
        (
            DirectorySpec(Path("/var/lib/sms-platform/redis/broker"), 999, 1000, 0o700),
            DirectorySpec(Path("/var/lib/sms-platform/redis/auth"), 999, 1000, 0o700),
            DirectorySpec(Path("/var/lib/sms-platform/redis/control"), 999, 1000, 0o700),
        ),
    ),
    RoleSpec(
        "runtime",
        200,
        Path("/var/lib/sms-platform/runtime"),
        0,
        0,
        0o750,
        "sms_runtime",
        (
            DirectorySpec(
                Path("/var/lib/sms-platform/runtime/imports"), 10001, 10001, 0o700
            ),
            DirectorySpec(
                Path("/var/lib/sms-platform/runtime/exports"), 10001, 10001, 0o700
            ),
            DirectorySpec(
                Path("/var/lib/sms-platform/runtime/raw-spill"), 10001, 10001, 0o700
            ),
            DirectorySpec(Path("/var/lib/sms-platform/runtime/backups"), 0, 0, 0o700),
        ),
    ),
)
SPECS_BY_ROLE = {spec.role: spec for spec in ROLE_SPECS}
DATA_SPECS = ROLE_SPECS[1:]
DATA_ROLES = cast(tuple[DataRole, ...], tuple(spec.role for spec in DATA_SPECS))
DOCKER_VOLUME_ROOT = Path("/var/lib/docker/volumes")
DOCKER_BIND_MOUNT_SPECS = (
    DockerBindMountSpec(
        "postgres",
        Path("/var/lib/sms-platform/postgres/pgdata"),
        DOCKER_VOLUME_ROOT / "sms-platform_pgdata" / "_data",
    ),
    DockerBindMountSpec(
        "redis",
        Path("/var/lib/sms-platform/redis/broker"),
        DOCKER_VOLUME_ROOT / "sms-platform_redisdata" / "_data",
    ),
    DockerBindMountSpec(
        "redis",
        Path("/var/lib/sms-platform/redis/auth"),
        DOCKER_VOLUME_ROOT / "sms-platform_redisauthdata" / "_data",
    ),
    DockerBindMountSpec(
        "redis",
        Path("/var/lib/sms-platform/redis/control"),
        DOCKER_VOLUME_ROOT / "sms-platform_rediscontroldata" / "_data",
    ),
    DockerBindMountSpec(
        "runtime",
        Path("/var/lib/sms-platform/runtime/imports"),
        DOCKER_VOLUME_ROOT / "sms-platform_importdata" / "_data",
    ),
    DockerBindMountSpec(
        "runtime",
        Path("/var/lib/sms-platform/runtime/exports"),
        DOCKER_VOLUME_ROOT / "sms-platform_exportdata" / "_data",
    ),
    DockerBindMountSpec(
        "runtime",
        Path("/var/lib/sms-platform/runtime/raw-spill"),
        DOCKER_VOLUME_ROOT / "sms-platform_rawspill" / "_data",
    ),
)


@dataclass(frozen=True, slots=True)
class ManifestDevice:
    by_id: Path
    expected_serial: str
    filesystem_uuid: str | None


@dataclass(frozen=True, slots=True)
class StorageManifest:
    change_id: str
    reviewer: str
    not_after: datetime
    devices: Mapping[Role, ManifestDevice]
    sha256: str


@dataclass(frozen=True, slots=True)
class BlockNode:
    path: Path
    device_type: str
    size_bytes: int
    serial: str
    wwn: str
    read_only: bool
    removable: bool
    major_minor: str
    filesystem: str
    label: str
    filesystem_uuid: str
    mountpoints: tuple[str, ...]
    children: tuple[BlockNode, ...]
    partition_table_type: str = ""
    partition_type: str = ""
    partition_uuid: str = ""
    partition_number: int = 0
    start_sector: int = 0
    logical_sector_size: int = 0


@dataclass(frozen=True, slots=True)
class DeviceObservation:
    role: Role
    by_id: Path
    resolved_path: Path
    size_bytes: int
    serial: str
    wwn: str
    major_minor: str
    filesystem: str
    label: str
    filesystem_uuid: str
    mountpoints: tuple[str, ...]
    identity_sha256: str

    def plan_payload(self) -> dict[str, object]:
        spec = SPECS_BY_ROLE[self.role]
        payload: dict[str, object] = {
            "by_id": str(self.by_id),
            "identity_sha256": self.identity_sha256,
            "major_minor": self.major_minor,
            "mount_path": str(spec.mount_path),
            "nominal_size_bytes": spec.nominal_bytes,
            "role": self.role,
            "serial": self.serial,
        }
        if self.role != "os":
            payload.update(
                {
                    "filesystem": "xfs",
                    "label": spec.label,
                    "mount_options": "defaults,nodev,nosuid",
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class LivePlan:
    canonical: Mapping[str, object]
    sha256: str
    observations: Mapping[Role, DeviceObservation]
    confirmation_token: str

    def public_payload(self) -> dict[str, object]:
        return {
            "action": "plan",
            **self.canonical,
            "confirmation_token": self.confirmation_token,
            "plan_sha256": self.sha256,
            "status": "ready",
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or XFS_UUID_RE.fullmatch(value) is None:
        raise StorageInitializationError("manifest_filesystem_uuid_invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise StorageInitializationError("manifest_filesystem_uuid_invalid") from exc
    if str(parsed) != value or parsed.version != 4:
        raise StorageInitializationError("manifest_filesystem_uuid_invalid")
    return value


def _parse_any_canonical_uuid(value: object, *, code: str) -> str:
    if not isinstance(value, str) or CANONICAL_UUID_RE.fullmatch(value) is None:
        raise StorageInitializationError(code)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise StorageInitializationError(code) from exc
    if str(parsed) != value:
        raise StorageInitializationError(code)
    return value


def _parse_not_after(
    value: object,
    *,
    now: datetime,
    allow_expired_recovery: bool,
) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise StorageInitializationError("manifest_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StorageInitializationError("manifest_expiry_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageInitializationError("manifest_expiry_invalid")
    normalized_now = now.astimezone(UTC)
    normalized_expiry = parsed.astimezone(UTC)
    if not allow_expired_recovery:
        if normalized_expiry <= normalized_now:
            raise StorageInitializationError("manifest_expired")
        if (normalized_expiry - normalized_now).total_seconds() > 24 * 60 * 60:
            raise StorageInitializationError("manifest_expiry_too_far")
    return parsed


def _safe_by_id(value: object) -> Path:
    if not isinstance(value, str) or "\x00" in value or "\n" in value:
        raise StorageInitializationError("manifest_by_id_invalid")
    path = Path(value)
    if (
        path.parent != Path("/dev/disk/by-id")
        or path.name in {"", ".", ".."}
        or BY_ID_BASENAME_RE.fullmatch(path.name) is None
        or re.search(r"-part[0-9]+$", path.name, re.IGNORECASE) is not None
    ):
        raise StorageInitializationError("manifest_by_id_invalid")
    return path


def parse_manifest(
    payload: bytes,
    *,
    now: datetime,
    allow_expired_recovery: bool = False,
) -> StorageManifest:
    """Parse the closed manifest schema used by plan/apply and exact-intent recovery.

    The recovery exception only relaxes the time window.  ``resume`` separately
    requires the unchanged raw manifest SHA-256 already sealed into durable intent.
    """

    if len(payload) > MAX_MANIFEST_BYTES:
        raise StorageInitializationError("manifest_too_large")
    try:
        raw = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageInitializationError("manifest_json_invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "change_id",
        "devices",
        "not_after",
        "reviewer",
        "schema_version",
    }:
        raise StorageInitializationError("manifest_schema_invalid")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise StorageInitializationError("manifest_schema_invalid")
    change_id = raw["change_id"]
    reviewer = raw["reviewer"]
    if (
        not isinstance(change_id, str)
        or SAFE_CHANGE_ID_RE.fullmatch(change_id) is None
        or ".." in change_id
    ):
        raise StorageInitializationError("manifest_change_id_invalid")
    if not isinstance(reviewer, str) or SAFE_REVIEWER_RE.fullmatch(reviewer) is None:
        raise StorageInitializationError("manifest_reviewer_invalid")
    raw_devices = raw["devices"]
    if not isinstance(raw_devices, dict) or set(raw_devices) != set(SPECS_BY_ROLE):
        raise StorageInitializationError("manifest_devices_invalid")
    devices: dict[Role, ManifestDevice] = {}
    filesystem_uuids: set[str] = set()
    by_ids: set[Path] = set()
    serials: set[str] = set()
    for spec in ROLE_SPECS:
        raw_device = raw_devices.get(spec.role)
        expected_keys = {"by_id", "expected_serial"}
        if spec.role != "os":
            expected_keys.add("filesystem_uuid")
        if not isinstance(raw_device, dict) or set(raw_device) != expected_keys:
            raise StorageInitializationError("manifest_device_schema_invalid")
        by_id = _safe_by_id(raw_device["by_id"])
        serial = raw_device["expected_serial"]
        if (
            not isinstance(serial, str)
            or SAFE_IDENTITY_RE.fullmatch(serial) is None
            or serial != serial.strip()
        ):
            raise StorageInitializationError("manifest_serial_invalid")
        filesystem_uuid = (
            _parse_canonical_uuid(raw_device["filesystem_uuid"])
            if spec.role != "os"
            else None
        )
        if by_id in by_ids or serial in serials:
            raise StorageInitializationError("manifest_device_identity_reused")
        by_ids.add(by_id)
        serials.add(serial)
        if filesystem_uuid is not None:
            if filesystem_uuid in filesystem_uuids:
                raise StorageInitializationError("manifest_filesystem_uuid_reused")
            filesystem_uuids.add(filesystem_uuid)
        devices[spec.role] = ManifestDevice(by_id, serial, filesystem_uuid)
    return StorageManifest(
        change_id=change_id,
        reviewer=reviewer,
        not_after=_parse_not_after(
            raw["not_after"],
            now=now,
            allow_expired_recovery=allow_expired_recovery,
        ),
        devices=devices,
        sha256=_sha256(payload),
    )


def _parse_bool(value: object, *, code: str) -> bool:
    if value is True or value in {1, "1"}:
        return True
    if value is False or value in {0, "0", None, ""}:
        return False
    raise StorageInitializationError(code)


def _parse_nonnegative_int(value: object, *, code: str) -> int:
    if isinstance(value, bool):
        raise StorageInitializationError(code)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        return int(value)
    raise StorageInitializationError(code)


def _parse_optional_nonnegative_int(value: object, *, code: str) -> int:
    if value is None or value == "":
        return 0
    return _parse_nonnegative_int(value, code=code)


def _parse_mountpoints(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw_values:
        if item is None or item == "":
            continue
        if not isinstance(item, str) or "\x00" in item or "\n" in item:
            raise StorageInitializationError("lsblk_mountpoints_invalid")
        result.append(item)
    return tuple(result)


def _parse_block_node(value: object) -> BlockNode:
    if not isinstance(value, dict):
        raise StorageInitializationError("lsblk_shape_invalid")
    try:
        path_value = value["path"]
        device_type = value["type"]
        major_minor = value["maj:min"]
    except KeyError as exc:
        raise StorageInitializationError("lsblk_shape_invalid") from exc
    if (
        not isinstance(path_value, str)
        or not path_value.startswith("/dev/")
        or "\x00" in path_value
        or not isinstance(device_type, str)
        or MAJOR_MINOR_RE.fullmatch(str(major_minor)) is None
    ):
        raise StorageInitializationError("lsblk_shape_invalid")
    children_value = value.get("children", [])
    if children_value is None:
        children_value = []
    if not isinstance(children_value, list):
        raise StorageInitializationError("lsblk_shape_invalid")
    string_fields: dict[str, str] = {}
    for field in (
        "serial",
        "wwn",
        "fstype",
        "label",
        "uuid",
        "pttype",
        "parttype",
        "partuuid",
    ):
        item = value.get(field)
        if item is None:
            string_fields[field] = ""
        elif not isinstance(item, str) or "\x00" in item or "\n" in item:
            raise StorageInitializationError("lsblk_shape_invalid")
        else:
            string_fields[field] = item
    return BlockNode(
        path=Path(path_value),
        device_type=device_type,
        size_bytes=_parse_nonnegative_int(value.get("size"), code="lsblk_size_invalid"),
        serial=string_fields["serial"],
        wwn=string_fields["wwn"],
        read_only=_parse_bool(value.get("ro"), code="lsblk_read_only_invalid"),
        removable=_parse_bool(value.get("rm"), code="lsblk_removable_invalid"),
        major_minor=str(major_minor),
        filesystem=string_fields["fstype"],
        label=string_fields["label"],
        filesystem_uuid=string_fields["uuid"].lower(),
        mountpoints=_parse_mountpoints(value.get("mountpoints")),
        children=tuple(_parse_block_node(child) for child in children_value),
        partition_table_type=string_fields["pttype"].lower(),
        partition_type=string_fields["parttype"].lower(),
        partition_uuid=string_fields["partuuid"].lower(),
        partition_number=_parse_optional_nonnegative_int(
            value.get("partn"),
            code="lsblk_partition_number_invalid",
        ),
        start_sector=_parse_optional_nonnegative_int(
            value.get("start"),
            code="lsblk_partition_start_invalid",
        ),
        logical_sector_size=_parse_optional_nonnegative_int(
            value.get("log-sec"),
            code="lsblk_sector_size_invalid",
        ),
    )


def parse_lsblk(payload: bytes) -> tuple[BlockNode, ...]:
    """Parse one bounded lsblk JSON snapshot without accepting partial records."""

    try:
        raw = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorageInitializationError("lsblk_json_invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"blockdevices"}:
        raise StorageInitializationError("lsblk_shape_invalid")
    devices = raw["blockdevices"]
    if not isinstance(devices, list) or not devices:
        raise StorageInitializationError("lsblk_shape_invalid")
    return tuple(_parse_block_node(item) for item in devices)


def _flatten_nodes(
    roots: Sequence[BlockNode],
) -> tuple[dict[str, BlockNode], dict[str, str | None]]:
    by_device: dict[str, BlockNode] = {}
    parents: dict[str, str | None] = {}

    def visit(node: BlockNode, parent: str | None) -> None:
        if node.major_minor in by_device:
            raise StorageInitializationError("lsblk_device_identity_reused")
        by_device[node.major_minor] = node
        parents[node.major_minor] = parent
        for child in node.children:
            visit(child, node.major_minor)

    for root in roots:
        visit(root, None)
    return by_device, parents


def _ancestor_chain(device: str, parents: Mapping[str, str | None]) -> frozenset[str]:
    result: set[str] = set()
    current: str | None = device
    while current is not None:
        if current in result or current not in parents:
            raise StorageInitializationError("block_topology_invalid")
        result.add(current)
        current = parents[current]
    return frozenset(result)


def _read_regular_secure(
    path: Path,
    *,
    maximum_bytes: int,
    expected_uid: int,
    modes: frozenset[int],
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StorageInitializationError("secure_file_unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_uid
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) not in modes
        ):
            raise StorageInitializationError("secure_file_unsafe")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise StorageInitializationError("secure_file_too_large")
        return payload, metadata
    finally:
        os.close(descriptor)


def _fsync_existing_regular_secure(
    path: Path,
    *,
    expected_payload: bytes,
    maximum_bytes: int,
    expected_uid: int,
    modes: frozenset[int],
    mismatch_code: str,
) -> None:
    """Revalidate and durably sync one already-published control file.

    This closes the recovery case where a previous process made a link or rename
    visible but died before the containing directory was durably committed.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StorageInitializationError("secure_file_unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_uid
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) not in modes
        ):
            raise StorageInitializationError("secure_file_unsafe")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise StorageInitializationError("secure_file_too_large")
        if payload != expected_payload:
            raise StorageInitializationError(mismatch_code)
        os.fsync(descriptor)
        linked = path.lstat()
        if (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise StorageInitializationError(mismatch_code)
    except OSError as exc:
        raise StorageInitializationError("secure_file_sync_failed") from exc
    finally:
        os.close(descriptor)
    try:
        _fsync_directory(path.parent)
        linked = path.lstat()
    except OSError as exc:
        raise StorageInitializationError("secure_file_sync_failed") from exc
    if (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise StorageInitializationError(mismatch_code)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_create(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    """Durably publish a new regular file without replacing any existing path."""

    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        _rename_noreplace(temporary, path)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise StorageInitializationError("atomic_target_exists") from exc
    except OSError as exc:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise StorageInitializationError("atomic_publish_failed") from exc
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _renameat2() -> object:
    """Return libc renameat2 or fail before any destructive storage action."""

    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise StorageInitializationError("rename_noreplace_unavailable") from exc
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically publish exactly one name without replacing an existing file."""

    function = _renameat2()
    result = function(  # type: ignore[operator]
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), target)


def _control_payload(value: Mapping[str, object]) -> bytes:
    payload = _canonical_json(value) + b"\n"
    if len(payload) > MAX_CONTROL_FILE_BYTES:
        raise StorageInitializationError("control_file_too_large")
    return payload


def _create_fixed_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise StorageInitializationError("directory_contract_mismatch")
        try:
            _fsync_directory(path)
            _fsync_directory(path.parent)
            current = path.lstat()
        except OSError as exc:
            raise StorageInitializationError("directory_sync_failed") from exc
        if (
            (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            or current.st_uid != uid
            or current.st_gid != gid
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise StorageInitializationError("directory_contract_mismatch")
        return
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise StorageInitializationError("directory_parent_unsafe")
    try:
        previous_umask = os.umask(0)
        try:
            os.mkdir(path, mode)
        finally:
            os.umask(previous_umask)
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        _fsync_directory(path)
        _fsync_directory(parent)
    except OSError as exc:
        raise StorageInitializationError("directory_creation_failed") from exc
    _create_fixed_directory(path, uid=uid, gid=gid, mode=mode)


def _assert_path_chain(path: Path, *, allow_missing: bool) -> bool:
    current = Path(path.anchor)
    missing = False
    for part in path.parts[1:]:
        current /= part
        if missing or not os.path.lexists(current):
            if not allow_missing:
                raise StorageInitializationError("path_unavailable")
            missing = True
            continue
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageInitializationError("path_chain_symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageInitializationError("path_chain_not_directory")
        if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
            raise StorageInitializationError("path_chain_permissions_unsafe")
    return not missing


def _directory_identity_without_symlinks(path: Path) -> tuple[int, int]:
    """Resolve an existing absolute directory without accepting symlink aliases."""

    if not path.is_absolute() or path.as_posix().startswith("//"):
        raise StorageInitializationError("fstab_target_not_canonical")
    current = Path(path.anchor)
    try:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise StorageInitializationError("fstab_target_symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise StorageInitializationError("fstab_target_not_directory")
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise StorageInitializationError("fstab_target_permissions_unsafe")
        if len(path.parts) > 1 and metadata.st_mode & 0o022:
            raise StorageInitializationError("fstab_target_permissions_unsafe")
        for index, part in enumerate(path.parts[1:], start=1):
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StorageInitializationError("fstab_target_symlink")
            if not stat.S_ISDIR(metadata.st_mode):
                raise StorageInitializationError("fstab_target_not_directory")
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
            ):
                raise StorageInitializationError("fstab_target_permissions_unsafe")
            if index < len(path.parts) - 1 and metadata.st_mode & 0o022:
                raise StorageInitializationError("fstab_target_permissions_unsafe")
    except FileNotFoundError as exc:
        raise StorageInitializationError("fstab_target_unavailable") from exc
    except OSError as exc:
        raise StorageInitializationError("fstab_target_inspection_failed") from exc
    return metadata.st_dev, metadata.st_ino


def _is_exact_swap_fstab_fields(fields: Sequence[str]) -> bool:
    return fields == [str(SWAP_FILE_PATH), "none", "swap", "sw", "0", "0"]


def _is_root_lvm_fstab_source(source: str) -> bool:
    path = Path(source)
    return (
        path.parent == Path("/dev/disk/by-id")
        and DM_LVM_BY_ID_BASENAME_RE.fullmatch(path.name) is not None
    )


def _assert_fstab_target_paths_safe(payload: bytes) -> None:
    """Reject non-existent, symlinked, or filesystem-identity-aliased targets."""

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise StorageInitializationError("fstab_encoding_invalid") from exc
    identities: dict[tuple[int, int], str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 6:
            raise StorageInitializationError("fstab_malformed")
        if fields[2] == "swap" or fields[1] == "none":
            if not _is_exact_swap_fstab_fields(fields):
                raise StorageInitializationError("fstab_swap_contract_invalid")
            continue
        target = fields[1]
        target_path = Path(target)
        if (
            not target.startswith("/")
            or target.startswith("//")
            or ".." in target_path.parts
            or target_path.as_posix() != target
        ):
            raise StorageInitializationError("fstab_target_not_canonical")
        identity = _directory_identity_without_symlinks(target_path)
        previous = identities.get(identity)
        if previous is not None and previous != target:
            raise StorageInitializationError("fstab_target_alias")
        identities[identity] = target

    # Before the managed block exists, also prove that no existing fstab target
    # is a bind-mounted alias of a fixed future target.
    for spec in DATA_SPECS:
        target = str(spec.mount_path)
        if not os.path.lexists(spec.mount_path):
            continue
        identity = _directory_identity_without_symlinks(spec.mount_path)
        previous = identities.get(identity)
        if previous is not None and previous != target:
            raise StorageInitializationError("fstab_target_alias")
        identities[identity] = target


def _assert_same_directory_object(left: Path, right: Path, *, code: str) -> None:
    """Require two paths to name the same live directory inode and device."""

    try:
        left_status = left.stat()
        right_status = right.stat()
    except OSError as exc:
        raise StorageInitializationError(code) from exc
    if (
        not stat.S_ISDIR(left_status.st_mode)
        or not stat.S_ISDIR(right_status.st_mode)
        or (left_status.st_dev, left_status.st_ino)
        != (right_status.st_dev, right_status.st_ino)
    ):
        raise StorageInitializationError(code)

def _directory_is_empty(path: Path) -> bool:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        with os.scandir(descriptor) as entries:
            return next(entries, None) is None
    finally:
        os.close(descriptor)


def _safe_identity_digest(
    *,
    by_id: Path,
    node: BlockNode,
) -> str:
    def topology(current: BlockNode) -> dict[str, object]:
        return {
            "children": [topology(child) for child in current.children],
            "filesystem": current.filesystem,
            "filesystem_uuid": current.filesystem_uuid,
            "label": current.label,
            "logical_sector_size": current.logical_sector_size,
            "mountpoints": list(current.mountpoints),
            "partition_number": current.partition_number,
            "partition_table_type": current.partition_table_type,
            "partition_type": current.partition_type,
            "partition_uuid": current.partition_uuid,
            "start_sector": current.start_sector,
            "type": current.device_type,
        }

    identity: dict[str, object] = {
        "by_id": str(by_id),
        "serial": node.serial,
        "wwn": node.wwn,
    }
    if node.children:
        identity["topology"] = topology(node)
    return _sha256(
        _canonical_json(identity)
    )


def render_fstab(original: bytes, manifest: StorageManifest, plan_sha256: str) -> bytes:
    """Append one exact managed block without changing any existing byte."""

    try:
        text = original.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise StorageInitializationError("fstab_encoding_invalid") from exc
    if "\x00" in text:
        raise StorageInitializationError("fstab_encoding_invalid")
    begin_prefix = "# BEGIN sms-platform production storage"
    end_marker = "# END sms-platform production storage"
    if begin_prefix in text or end_marker in text:
        raise StorageInitializationError("fstab_managed_block_exists")
    existing_targets: set[str] = set()
    existing_sources: set[str] = set()
    root_entries = 0
    forbidden_internal = Path("/var/lib/docker/volumes")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 6:
            raise StorageInitializationError("fstab_malformed")
        source, target, fs_type, options, dump, pass_number = fields
        if fs_type == "swap" or target == "none":
            if not _is_exact_swap_fstab_fields(fields):
                raise StorageInitializationError("fstab_swap_contract_invalid")
            if source.casefold() in existing_sources or target in existing_targets:
                raise StorageInitializationError("fstab_swap_contract_invalid")
            existing_sources.add(source.casefold())
            existing_targets.add(target)
            continue
        target_path = Path(target)
        source_path = Path(source) if source.startswith("/") else None
        if (
            not target.startswith("/")
            or target.startswith("//")
            or ".." in target_path.parts
            or target_path.as_posix() != target
        ):
            raise StorageInitializationError("fstab_target_not_canonical")
        if source_path is not None and (
            source.startswith("//")
            or ".." in source_path.parts
            or source_path.as_posix() != source
        ):
            raise StorageInitializationError("fstab_source_not_canonical")
        if (
            target_path == forbidden_internal
            or forbidden_internal in target_path.parents
            or (
                source_path is not None
                and (source_path == forbidden_internal or forbidden_internal in source_path.parents)
            )
        ):
            raise StorageInitializationError("fstab_docker_internal_path")
        if target in existing_targets:
            raise StorageInitializationError("fstab_target_reused")
        if source.casefold() in existing_sources:
            raise StorageInitializationError("fstab_source_reused")
        existing_targets.add(target)
        existing_sources.add(source.casefold())
        if target == "/":
            root_entries += 1
            if (
                not _is_root_lvm_fstab_source(source)
                or fs_type != "ext4"
                or set(options.split(",")) != {"defaults"}
                or dump != "0"
                or pass_number != "1"
            ):
                raise StorageInitializationError("fstab_root_contract_invalid")
    if root_entries != 1:
        raise StorageInitializationError("fstab_root_contract_invalid")
    lines = [f"{begin_prefix} {plan_sha256[:16]}"]
    for spec in DATA_SPECS:
        device = manifest.devices[spec.role]
        assert device.filesystem_uuid is not None
        if str(spec.mount_path) in existing_targets:
            raise StorageInitializationError("fstab_target_conflict")
        source = f"UUID={device.filesystem_uuid}"
        if source.casefold() in existing_sources:
            raise StorageInitializationError("fstab_uuid_conflict")
        lines.append(
            f"{source} {spec.mount_path} xfs defaults,nodev,nosuid 0 2"
        )
    lines.append(end_marker)
    separator = b"" if original.endswith(b"\n") else b"\n"
    return original + separator + ("\n".join(lines) + "\n").encode("ascii")


class ConfirmationReader(Protocol):
    def confirm(self, plan: LivePlan) -> None: ...


class TtyConfirmationReader:
    """Require confirmations from the controlling TTY, never argv or stdin."""

    def confirm(self, plan: LivePlan) -> None:
        try:
            descriptor = os.open(
                "/dev/tty",
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0),
            )
        except OSError as exc:
            raise StorageInitializationError("controlling_tty_required") from exc
        try:
            if not os.isatty(descriptor):
                raise StorageInitializationError("controlling_tty_required")
            with (
                os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as tty_input,
                os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as tty_output,
            ):
                for role in DATA_ROLES:
                    observation = plan.observations[role]
                    tty_output.write(
                        f"确认 {role} 数据盘，输入完整序列号 {observation.serial}: "
                    )
                    tty_output.flush()
                    if tty_input.readline(512).rstrip("\r\n") != observation.serial:
                        raise StorageInitializationError("interactive_confirmation_failed")
                tty_output.write(f"最终确认，输入 {plan.confirmation_token}: ")
                tty_output.flush()
                if tty_input.readline(512).rstrip("\r\n") != plan.confirmation_token:
                    raise StorageInitializationError("interactive_confirmation_failed")
        finally:
            os.close(descriptor)


class ProductionStorageInitializer:
    """Plan, apply, resume and inspect the fixed production storage contract."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        confirmation_reader: ConfirmationReader | None = None,
        manifest_path: Path = MANIFEST_DEFAULT,
        state_path: Path = STATE_PATH,
        intent_path: Path = INTENT_PATH,
        lock_path: Path = LOCK_PATH,
        fstab_path: Path = FSTAB_PATH,
        effective_uid: int | None = None,
        expected_uid: int = 0,
        now: datetime | None = None,
        script_path: Path | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.confirmation_reader = confirmation_reader or TtyConfirmationReader()
        self.manifest_path = manifest_path
        self.state_path = state_path
        self.intent_path = intent_path
        self.lock_path = lock_path
        self.fstab_path = fstab_path
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self.expected_uid = expected_uid
        self._fixed_now = now
        self.now = now or datetime.now(UTC)
        self.script_path = script_path or Path(__file__)
        self._active_lock_fd: int | None = None

    def _current_time(self) -> datetime:
        return self._fixed_now or datetime.now(UTC)

    def _run(
        self,
        argv: Sequence[str],
        *,
        code: str,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
        pass_fds: Sequence[int] = (),
    ) -> CommandResult:
        result = self.runner.run(
            argv,
            timeout_seconds=timeout_seconds,
            pass_fds=pass_fds,
        )
        if result.returncode not in allowed_returncodes:
            raise StorageInitializationError(code)
        return result

    def _read_manifest(
        self,
        *,
        allow_expired_recovery: bool = False,
    ) -> StorageManifest:
        payload, _metadata = _read_regular_secure(
            self.manifest_path,
            maximum_bytes=MAX_MANIFEST_BYTES,
            expected_uid=self.expected_uid,
            modes=frozenset({0o600}),
        )
        return parse_manifest(
            payload,
            now=self._current_time(),
            allow_expired_recovery=allow_expired_recovery,
        )

    def _script_sha256(self) -> str:
        payload, metadata = _read_regular_secure(
            self.script_path,
            maximum_bytes=2 * 1024 * 1024,
            expected_uid=self.expected_uid,
            modes=frozenset({0o755}),
        )
        if metadata.st_mode & 0o022:
            raise StorageInitializationError("script_file_unsafe")
        return _sha256(payload)

    def _host_identity(self) -> dict[str, str]:
        machine, _ = _read_regular_secure(
            MACHINE_ID_PATH,
            maximum_bytes=256,
            expected_uid=0,
            modes=frozenset({0o444, 0o644}),
        )
        boot, _ = _read_regular_secure(
            BOOT_ID_PATH,
            maximum_bytes=256,
            expected_uid=0,
            modes=frozenset({0o444, 0o400}),
        )
        product, _ = _read_regular_secure(
            PRODUCT_UUID_PATH,
            maximum_bytes=256,
            expected_uid=0,
            modes=frozenset({0o444, 0o400}),
        )
        values = {
            "boot_id": boot.decode("ascii", errors="strict").strip().lower(),
            "machine_id_sha256": _sha256(machine.strip()),
            "product_uuid_sha256": _sha256(product.strip().lower()),
        }
        if XFS_UUID_RE.fullmatch(values["boot_id"]) is None:
            # Boot IDs are UUIDs but are not required to be version 4.
            try:
                uuid.UUID(values["boot_id"])
            except ValueError as exc:
                raise StorageInitializationError("host_identity_invalid") from exc
        return values

    def _require_real_vm_host(self) -> None:
        if self.effective_uid != 0:
            raise StorageInitializationError("root_required")
        if sys.version_info[:2] != (3, 12) or os.uname().machine not in {
            "amd64",
            "x86_64",
        }:
            raise StorageInitializationError("production_os_runtime_required")
        release, _ = _read_regular_secure(
            OS_RELEASE_PATH,
            maximum_bytes=64 * 1024,
            expected_uid=0,
            modes=frozenset({0o444, 0o644}),
        )
        release_fields: dict[str, str] = {}
        try:
            release_text = release.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StorageInitializationError("production_os_runtime_required") from exc
        for raw_line in release_text.splitlines():
            if not raw_line or raw_line.lstrip().startswith("#"):
                continue
            key, separator, raw_value = raw_line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise StorageInitializationError("production_os_runtime_required")
            if key in release_fields:
                raise StorageInitializationError("production_os_runtime_required")
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            release_fields[key] = value
        if (
            release_fields.get("ID") != "ubuntu"
            or release_fields.get("VERSION_ID") != "24.04"
            or release_fields.get("VERSION_CODENAME") != "noble"
        ):
            raise StorageInitializationError("production_os_runtime_required")
        try:
            if os.readlink(PID1_MOUNT_NS) != os.readlink(SELF_MOUNT_NS):
                raise StorageInitializationError("private_mount_namespace_forbidden")
        except OSError as exc:
            raise StorageInitializationError("mount_namespace_unavailable") from exc
        _assert_same_directory_object(
            HOST_ROOT_PATH,
            PID1_ROOT_PATH,
            code="chroot_execution_forbidden",
        )
        pid1, _ = _read_regular_secure(
            PID1_COMM_PATH,
            maximum_bytes=256,
            expected_uid=0,
            modes=frozenset({0o644, 0o444, 0o400}),
        )
        if pid1.decode("ascii", errors="strict").strip() != "systemd":
            raise StorageInitializationError("systemd_pid1_required")
        self._run(
            (SYSTEMD_DETECT_VIRT_BINARY, "--quiet", "--container"),
            code="container_execution_forbidden",
            allowed_returncodes=frozenset({1}),
        )
        virtualization = self._run(
            (SYSTEMD_DETECT_VIRT_BINARY, "--vm"),
            code="vmware_virtualization_required",
        )
        if virtualization.stdout.decode("ascii", errors="strict").strip() != "vmware":
            raise StorageInitializationError("vmware_virtualization_required")
        vendor, _ = _read_regular_secure(
            SYS_VENDOR_PATH,
            maximum_bytes=256,
            expected_uid=0,
            modes=frozenset({0o444, 0o400}),
        )
        if vendor.decode("ascii", errors="strict").strip() != "VMware, Inc.":
            raise StorageInitializationError("vmware_host_required")
        for target, expected_fs in (("/proc", "proc"), ("/sys", "sysfs"), ("/dev", "devtmpfs")):
            result = self._run(
                (
                    FINDMNT_BINARY,
                    "--noheadings",
                    "--output",
                    "FSTYPE",
                    "--target",
                    target,
                ),
                code="host_mount_boundary_invalid",
            )
            if result.stdout.decode("ascii", errors="strict").strip() != expected_fs:
                raise StorageInitializationError("host_mount_boundary_invalid")

    def _require_tools(self) -> None:
        for binary in (
            LSBLK_BINARY,
            FINDMNT_BINARY,
            BLKID_BINARY,
            WIPEFS_BINARY,
            MKFS_XFS_BINARY,
            XFS_INFO_BINARY,
            MOUNT_BINARY,
            SYSTEMCTL_BINARY,
            SYSTEMD_DETECT_VIRT_BINARY,
            UDEVADM_BINARY,
            TIMEDATECTL_BINARY,
        ):
            try:
                metadata = Path(binary).stat()
            except OSError as exc:
                raise StorageInitializationError("required_tool_unavailable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
                or not metadata.st_mode & stat.S_IXUSR
            ):
                raise StorageInitializationError("required_tool_unsafe")
        _renameat2()

    def _require_time_contract(self) -> None:
        timezone = self._run(
            (
                TIMEDATECTL_BINARY,
                "show",
                "--property=Timezone",
                "--value",
            ),
            code="time_contract_unavailable",
        )
        synchronized = self._run(
            (
                TIMEDATECTL_BINARY,
                "show",
                "--property=NTPSynchronized",
                "--value",
            ),
            code="time_contract_unavailable",
        )
        try:
            timezone_value = timezone.stdout.decode("ascii", errors="strict").strip()
            synchronized_value = (
                synchronized.stdout.decode("ascii", errors="strict").strip().lower()
            )
        except UnicodeError as exc:
            raise StorageInitializationError("time_contract_invalid") from exc
        if timezone_value != "Asia/Shanghai" or synchronized_value != "yes":
            raise StorageInitializationError("time_contract_invalid")

    def _require_services_stopped(self) -> None:
        for unit in (
            "docker.service",
            "docker.socket",
            "containerd.service",
            "sms-platform.service",
        ):
            load = self._run(
                (
                    SYSTEMCTL_BINARY,
                    "show",
                    "--property=LoadState",
                    "--value",
                    unit,
                ),
                code="service_state_unavailable",
            )
            load_state = load.stdout.decode("ascii", errors="strict").strip()
            if load_state not in {"masked", "not-found"}:
                raise StorageInitializationError("service_not_masked")
            active = self._run(
                (SYSTEMCTL_BINARY, "is-active", unit),
                code="service_state_unavailable",
                allowed_returncodes=frozenset({0, 3, 4}),
            )
            if active.returncode == 0:
                raise StorageInitializationError("service_active")

    def _require_root_filesystem_contract(self) -> None:
        result = self._run(
            (
                FINDMNT_BINARY,
                "--noheadings",
                "--output",
                "FSTYPE",
                "--target",
                "/",
            ),
            code="root_filesystem_unavailable",
        )
        if result.stdout.decode("ascii", errors="strict").strip() != "ext4":
            raise StorageInitializationError("root_filesystem_must_be_ext4")
        try:
            root = Path("/").lstat()
        except OSError as exc:
            raise StorageInitializationError("root_filesystem_unavailable") from exc
        capacity = self._root_filesystem_capacity()
        if (
            not stat.S_ISDIR(root.st_mode)
            or root.st_uid != 0
            or root.st_gid != 0
            or stat.S_IMODE(root.st_mode) != 0o755
            or capacity < OS_ROOT_FILESYSTEM_MINIMUM_BYTES
        ):
            raise StorageInitializationError("root_filesystem_contract_mismatch")
        self._require_swap_file_contract(root_device=root.st_dev)

    def _require_swap_file_contract(self, *, root_device: int) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(SWAP_FILE_PATH, flags)
        except OSError as exc:
            raise StorageInitializationError("swap_file_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            # Noble's installer-created active swap file uses preallocated extents;
            # ext4 may report those as SEEK_HOLE even though swapon has accepted them.
            # Full st_blocks coverage plus the exact active /proc/swaps entry below
            # is the fail-closed allocation proof for this frozen host contract.
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != root_device
                or metadata.st_size != SWAP_FILE_BYTES
                or metadata.st_blocks * 512 < metadata.st_size
            ):
                raise StorageInitializationError("swap_file_contract_mismatch")
        finally:
            os.close(descriptor)

        swaps, _ = _read_regular_secure(
            PROC_SWAPS_PATH,
            maximum_bytes=64 * 1024,
            expected_uid=0,
            modes=frozenset({0o444, 0o644}),
        )
        try:
            swap_lines = [
                line
                for line in swaps.decode("ascii", errors="strict").splitlines()
                if line.strip()
            ]
        except UnicodeError as exc:
            raise StorageInitializationError("swap_state_invalid") from exc
        if (
            not swap_lines
            or swap_lines[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]
        ):
            raise StorageInitializationError("swap_state_invalid")
        if len(swap_lines) != 2:
            raise StorageInitializationError("swap_state_contract_mismatch")
        fields = swap_lines[1].split()
        if len(fields) != 5:
            raise StorageInitializationError("swap_state_contract_mismatch")
        try:
            active_size_bytes = int(fields[2]) * 1024
            used_bytes = int(fields[3]) * 1024
            priority = int(fields[4])
        except ValueError as exc:
            raise StorageInitializationError("swap_state_contract_mismatch") from exc
        if (
            fields[0] != str(SWAP_FILE_PATH)
            or fields[1] != "file"
            or active_size_bytes < SWAP_FILE_BYTES - SWAP_ACTIVE_HEADER_MAX_BYTES
            or active_size_bytes >= SWAP_FILE_BYTES
            or used_bytes < 0
            or used_bytes > active_size_bytes
            or priority != -2
        ):
            raise StorageInitializationError("swap_state_contract_mismatch")

    def _root_filesystem_capacity(self) -> int:
        try:
            filesystem = os.statvfs("/")
        except OSError as exc:
            raise StorageInitializationError("root_filesystem_unavailable") from exc
        return filesystem.f_frsize * filesystem.f_blocks

    def _lsblk(self) -> tuple[BlockNode, ...]:
        result = self._run(
            (
                LSBLK_BINARY,
                "--bytes",
                "--json",
                "--paths",
                "--output",
                "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,SERIAL,WWN,RO,RM,MAJ:MIN,PTTYPE,PARTTYPE,PARTUUID,PARTN,START,LOG-SEC",
            ),
            code="lsblk_failed",
        )
        return parse_lsblk(result.stdout)

    def _root_major_minor(self) -> str:
        result = self._run(
            (
                FINDMNT_BINARY,
                "--noheadings",
                "--output",
                "MAJ:MIN",
                "--target",
                "/",
            ),
            code="root_device_unavailable",
        )
        value = result.stdout.decode("ascii", errors="strict").strip()
        if MAJOR_MINOR_RE.fullmatch(value) is None:
            raise StorageInitializationError("root_device_invalid")
        return value

    def _resolve_by_id(self, path: Path) -> tuple[Path, os.stat_result]:
        try:
            link_metadata = path.lstat()
            if (
                not stat.S_ISLNK(link_metadata.st_mode)
                or link_metadata.st_uid != 0
                or link_metadata.st_gid != 0
            ):
                raise StorageInitializationError("by_id_symlink_unsafe")
            target = os.readlink(path)
            resolved = Path(target) if Path(target).is_absolute() else path.parent / target
            resolved = Path(os.path.normpath(resolved))
            if resolved.parent != Path("/dev"):
                raise StorageInitializationError("by_id_target_invalid")
            metadata = resolved.lstat()
        except OSError as exc:
            raise StorageInitializationError("by_id_unavailable") from exc
        if not stat.S_ISBLK(metadata.st_mode):
            raise StorageInitializationError("by_id_not_block_device")
        return resolved, metadata

    def _assert_no_holders(self, major_minor: str) -> None:
        path = Path("/sys/dev/block") / major_minor / "holders"
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                with os.scandir(descriptor) as entries:
                    if next(entries, None) is not None:
                        raise StorageInitializationError("device_has_holders")
            finally:
                os.close(descriptor)
        except StorageInitializationError:
            raise
        except OSError as exc:
            raise StorageInitializationError("device_holders_unavailable") from exc

    def _assert_blank_signatures(
        self,
        path: Path,
        *,
        pass_fds: Sequence[int] = (),
    ) -> None:
        wipefs = self._run(
            (WIPEFS_BINARY, "--no-act", "--json", str(path)),
            code="wipefs_probe_failed",
            pass_fds=pass_fds,
        )
        try:
            raw = json.loads(wipefs.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StorageInitializationError("wipefs_output_invalid") from exc
        if not isinstance(raw, dict) or set(raw) != {"signatures"}:
            raise StorageInitializationError("wipefs_output_invalid")
        signatures = raw["signatures"]
        if not isinstance(signatures, list) or signatures:
            raise StorageInitializationError("device_signature_present")
        blkid = self._run(
            (BLKID_BINARY, "-p", "-o", "export", str(path)),
            code="blkid_blank_probe_failed",
            allowed_returncodes=frozenset({2}),
            pass_fds=pass_fds,
        )
        if blkid.stdout or blkid.stderr:
            raise StorageInitializationError("blkid_blank_probe_invalid")

    def _assert_zero_samples_descriptor(self, descriptor: int, size_bytes: int) -> None:
        try:
            metadata_before = os.fstat(descriptor)
            if not stat.S_ISBLK(metadata_before.st_mode):
                raise StorageInitializationError("device_sample_invalid")
            raw_size = fcntl.ioctl(descriptor, BLKGETSIZE64, b"\0" * 8)
            if struct.unpack("=Q", raw_size)[0] != size_bytes:
                raise StorageInitializationError("device_size_mismatch")
            maximum_offset = size_bytes - SAMPLE_BYTES
            offsets = {0, maximum_offset}
            for index in range(1, SAMPLE_COUNT - 1):
                offsets.add(maximum_offset * index // (SAMPLE_COUNT - 1))
            for offset in sorted(offsets):
                sample = os.pread(descriptor, SAMPLE_BYTES, offset)
                if len(sample) != SAMPLE_BYTES:
                    raise StorageInitializationError("device_sample_short_read")
                if any(sample):
                    raise StorageInitializationError("device_sample_not_zero")
            metadata_after = os.fstat(descriptor)
            if (
                metadata_after.st_rdev != metadata_before.st_rdev
                or metadata_after.st_size != metadata_before.st_size
            ):
                raise StorageInitializationError("device_identity_changed")
        except OSError as exc:
            raise StorageInitializationError("device_sample_failed") from exc

    def _assert_zero_samples(self, path: Path, size_bytes: int) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StorageInitializationError("device_sample_unavailable") from exc
        try:
            self._assert_zero_samples_descriptor(descriptor, size_bytes)
        finally:
            os.close(descriptor)

    def _block_device_size(self, path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StorageInitializationError("device_size_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISBLK(metadata.st_mode):
                raise StorageInitializationError("device_size_invalid")
            raw_size = fcntl.ioctl(descriptor, BLKGETSIZE64, b"\0" * 8)
            return cast(int, struct.unpack("=Q", raw_size)[0])
        except OSError as exc:
            raise StorageInitializationError("device_size_ioctl_failed") from exc
        finally:
            os.close(descriptor)

    def _assert_storage_parent_contract(self, *, allow_missing: bool) -> bool:
        sms_root_exists = _assert_path_chain(
            SMS_STORAGE_ROOT,
            allow_missing=allow_missing,
        )
        if sms_root_exists:
            metadata = SMS_STORAGE_ROOT.lstat()
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o750
            ):
                raise StorageInitializationError("storage_parent_contract_mismatch")
        return sms_root_exists

    def _inspect_mountpoint_candidates(self) -> None:
        self._assert_storage_parent_contract(allow_missing=True)
        for spec in DATA_SPECS:
            exists = _assert_path_chain(spec.mount_path, allow_missing=True)
            if not exists:
                continue
            metadata = spec.mount_path.lstat()
            if (
                metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != spec.mount_mode
                or not _directory_is_empty(spec.mount_path)
            ):
                raise StorageInitializationError("mountpoint_not_empty_or_unsafe")

    def _root_filesystem_uuid(self) -> str:
        result = self._run(
            (
                FINDMNT_BINARY,
                "--noheadings",
                "--output",
                "UUID",
                "--target",
                "/",
            ),
            code="root_filesystem_uuid_unavailable",
        )
        try:
            value = result.stdout.decode("ascii", errors="strict").strip().lower()
        except UnicodeError as exc:
            raise StorageInitializationError("root_filesystem_uuid_invalid") from exc
        return _parse_any_canonical_uuid(
            value,
            code="root_filesystem_uuid_invalid",
        )

    def _verify_current_fstab_semantics(self) -> None:
        self._verify_fstab_file(
            self.fstab_path,
            code="fstab_current_verification_failed",
        )

    def _verify_fstab_file(self, path: Path, *, code: str) -> None:
        result = self._run(
            (
                FINDMNT_BINARY,
                "--verify",
                "--tab-file",
                str(path),
            ),
            code=code,
        )
        if not _findmnt_verify_output_is_accepted(result.stdout, result.stderr):
            raise StorageInitializationError(code)

    def _inspect_fstab(self) -> tuple[bytes, os.stat_result, str]:
        payload, metadata = _read_regular_secure(
            self.fstab_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600, 0o644}),
        )
        # Render with a fixed dummy digest to validate every existing line and conflict.
        dummy_devices: dict[Role, ManifestDevice] = {
            "os": ManifestDevice(Path("/dev/disk/by-id/dummy-os"), "dummy-os", None)
        }
        for index, spec in enumerate(DATA_SPECS, start=1):
            dummy_devices[spec.role] = ManifestDevice(
                Path(f"/dev/disk/by-id/dummy-{spec.role}"),
                f"dummy-{spec.role}",
                f"00000000-0000-4000-8000-{index:012d}",
            )
        dummy = StorageManifest(
            "CHK-DUMMY",
            "reviewer",
            self.now,
            dummy_devices,
            "0" * 64,
        )
        render_fstab(payload, dummy, "0" * 64)
        _assert_fstab_target_paths_safe(payload)
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StorageInitializationError("fstab_encoding_invalid") from exc
        required: dict[str, tuple[str, str, str, str, str, str]] = {}
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != 6:
                raise StorageInitializationError("fstab_malformed")
            target = fields[1]
            if target in required:
                raise StorageInitializationError("fstab_target_reused")
            required[target] = cast(tuple[str, str, str, str, str, str], tuple(fields))
        if set(required) != {"/", "/boot", "/boot/efi", "none"}:
            raise StorageInitializationError("fstab_os_contract_invalid")
        root_fields = required["/"]
        boot_fields = required["/boot"]
        efi_fields = required["/boot/efi"]
        swap_fields = required["none"]
        boot_source = Path(boot_fields[0])
        efi_source = Path(efi_fields[0])
        if (
            not _is_root_lvm_fstab_source(root_fields[0])
            or root_fields[2:] != ("ext4", "defaults", "0", "1")
            or boot_source.parent != Path("/dev/disk/by-uuid")
            or CANONICAL_UUID_RE.fullmatch(boot_source.name) is None
            or boot_fields[2:] != ("ext4", "defaults", "0", "1")
            or efi_source.parent != Path("/dev/disk/by-uuid")
            or FAT_UUID_RE.fullmatch(efi_source.name) is None
            or efi_fields[2:] != ("vfat", "defaults", "0", "1")
            or not _is_exact_swap_fstab_fields(list(swap_fields))
        ):
            raise StorageInitializationError("fstab_os_contract_invalid")
        for target, fields in (
            (Path("/"), root_fields),
            (Path("/boot"), boot_fields),
            (Path("/boot/efi"), efi_fields),
        ):
            source_major_minor = self._fstab_source_major_minor(fields[0], fields[2])
            mounted = self._findmnt_record(target)
            if source_major_minor != mounted.get("maj:min"):
                raise StorageInitializationError("fstab_mount_identity_mismatch")
        root_uuid = self._root_filesystem_uuid()
        self._verify_current_fstab_semantics()
        return payload, metadata, root_uuid

    def _fstab_source_major_minor(self, source: str, fs_type: str) -> str | None:
        if fs_type == "swap" and source == str(SWAP_FILE_PATH):
            return None
        path: Path | None = None
        if source.startswith("/dev/"):
            path = Path(source)
        elif source.startswith("UUID=") or source.startswith("LABEL="):
            key, value = source.split("=", 1)
            if SAFE_FSTAB_TOKEN_RE.fullmatch(value) is None:
                raise StorageInitializationError("fstab_source_invalid")
            option = "-U" if key == "UUID" else "-L"
            resolved = self._run(
                (BLKID_BINARY, option, value),
                code="fstab_source_unresolvable",
            )
            try:
                decoded = resolved.stdout.decode("ascii", errors="strict").strip()
            except UnicodeError as exc:
                raise StorageInitializationError("fstab_source_unresolvable") from exc
            if "\n" in decoded or not decoded.startswith("/dev/"):
                raise StorageInitializationError("fstab_source_unresolvable")
            path = Path(decoded)
        elif source.startswith("PARTUUID=") or source.startswith("PARTLABEL="):
            key, value = source.split("=", 1)
            if SAFE_FSTAB_TOKEN_RE.fullmatch(value) is None:
                raise StorageInitializationError("fstab_source_invalid")
            directory = "by-partuuid" if key == "PARTUUID" else "by-partlabel"
            path = Path("/dev/disk") / directory / value
        elif fs_type in PSEUDO_FSTAB_TYPES:
            return None
        else:
            raise StorageInitializationError("fstab_source_unsupported")
        try:
            metadata = path.stat()
        except OSError as exc:
            raise StorageInitializationError("fstab_source_unresolvable") from exc
        if not stat.S_ISBLK(metadata.st_mode):
            raise StorageInitializationError("fstab_source_not_block_device")
        return f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"

    def _assert_fstab_sources_disjoint_from_data(
        self,
        payload: bytes,
        observations: Mapping[Role, DeviceObservation],
    ) -> None:
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StorageInitializationError("fstab_encoding_invalid") from exc
        protected = {observations[role].major_minor for role in DATA_ROLES}
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) != 6:
                raise StorageInitializationError("fstab_malformed")
            major_minor = self._fstab_source_major_minor(fields[0], fields[2])
            if major_minor in protected:
                raise StorageInitializationError("fstab_references_data_device")

    def _assert_fstab_matches_plan(self, plan: Mapping[str, object]) -> None:
        expected = plan.get("fstab")
        if not isinstance(expected, dict) or set(expected) != {"inode", "mode", "sha256"}:
            raise StorageInitializationError("stored_plan_invalid")
        payload, metadata = _read_regular_secure(
            self.fstab_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600, 0o644}),
        )
        if (
            expected.get("inode") != metadata.st_ino
            or expected.get("mode") != f"{stat.S_IMODE(metadata.st_mode):04o}"
            or expected.get("sha256") != _sha256(payload)
        ):
            raise StorageInitializationError("fstab_changed_since_plan")
        _assert_fstab_target_paths_safe(payload)
        self._verify_current_fstab_semantics()

    @contextmanager
    def _open_revalidated_blank_device(
        self,
        observation: DeviceObservation,
    ) -> Iterator[int]:
        """Revalidate and pin the exact block object used by ``mkfs.xfs``.

        The parent deliberately does not use ``O_EXCL``: xfsprogs acquires its own
        exclusive claim when it reopens the inherited ``/proc/self/fd/N`` path.
        """

        lock_descriptor = self._active_lock_fd
        if lock_descriptor is None:
            raise StorageInitializationError("destructive_lock_required")
        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(observation.resolved_path, flags)
        except OSError as exc:
            raise StorageInitializationError("device_pin_failed") from exc
        try:
            claimed = os.fstat(descriptor)
            if not stat.S_ISBLK(claimed.st_mode):
                raise StorageInitializationError("device_pin_invalid")
            claimed_major_minor = (
                f"{os.major(claimed.st_rdev)}:{os.minor(claimed.st_rdev)}"
            )
            resolved, linked = self._resolve_by_id(observation.by_id)
            roots = self._lsblk()
            by_device, _parents = _flatten_nodes(roots)
            node = by_device.get(claimed_major_minor)
            if (
                claimed.st_rdev != linked.st_rdev
                or resolved != observation.resolved_path
                or claimed_major_minor != observation.major_minor
                or node is None
                or node.path != resolved
                or node.device_type != "disk"
                or node.read_only
                or node.removable
                or node.children
                or node.size_bytes != observation.size_bytes
                or node.serial != observation.serial
                or node.wwn != observation.wwn
                or node.filesystem
                or node.label
                or node.filesystem_uuid
                or node.mountpoints
                or _safe_identity_digest(by_id=observation.by_id, node=node)
                != observation.identity_sha256
            ):
                raise StorageInitializationError("device_changed_before_mkfs")
            self._assert_no_holders(claimed_major_minor)
            inherited = (descriptor, lock_descriptor)
            descriptor_path = Path(f"/proc/self/fd/{descriptor}")
            self._assert_blank_signatures(descriptor_path, pass_fds=inherited)
            self._assert_zero_samples_descriptor(descriptor, observation.size_bytes)
            yield descriptor
            after = os.fstat(descriptor)
            if after.st_rdev != claimed.st_rdev:
                raise StorageInitializationError("device_identity_changed_during_mkfs")
            raw_size = fcntl.ioctl(descriptor, BLKGETSIZE64, b"\0" * 8)
            if struct.unpack("=Q", raw_size)[0] != observation.size_bytes:
                raise StorageInitializationError("device_size_changed_during_mkfs")
        except StorageInitializationError:
            raise
        except OSError as exc:
            raise StorageInitializationError("device_claim_readback_failed") from exc
        finally:
            os.close(descriptor)

    def _assert_os_disk_layout(
        self,
        os_disk: BlockNode,
        *,
        root_major_minor: str,
        parents: Mapping[str, str | None],
    ) -> None:
        if (
            os_disk.partition_table_type != "gpt"
            or os_disk.logical_sector_size != OS_LOGICAL_SECTOR_BYTES
            or len(os_disk.children) != 3
        ):
            raise StorageInitializationError("os_disk_layout_invalid")
        by_partition_number = {
            child.partition_number: child for child in os_disk.children
        }
        if set(by_partition_number) != {
            OS_EFI_PARTITION_NUMBER,
            OS_BOOT_PARTITION_NUMBER,
            OS_LVM_PARTITION_NUMBER,
        }:
            raise StorageInitializationError("os_disk_layout_invalid")
        efi = by_partition_number[OS_EFI_PARTITION_NUMBER]
        boot = by_partition_number[OS_BOOT_PARTITION_NUMBER]
        lvm_partition = by_partition_number[OS_LVM_PARTITION_NUMBER]
        if len(lvm_partition.children) != 1:
            raise StorageInitializationError("os_disk_layout_invalid")
        root = lvm_partition.children[0]
        try:
            partition_uuids = tuple(
                str(uuid.UUID(child.partition_uuid)) for child in os_disk.children
            )
        except ValueError as exc:
            raise StorageInitializationError("os_disk_layout_invalid") from exc
        lvm_partition_end = (
            lvm_partition.start_sector * OS_LOGICAL_SECTOR_BYTES
            + lvm_partition.size_bytes
        )
        if (
            len(set(partition_uuids)) != 3
            or partition_uuids
            != tuple(child.partition_uuid for child in os_disk.children)
            or efi.device_type != "part"
            or efi.logical_sector_size != OS_LOGICAL_SECTOR_BYTES
            or efi.start_sector != OS_EFI_PARTITION_START_SECTOR
            or efi.size_bytes != OS_EFI_PARTITION_BYTES
            or efi.filesystem != "vfat"
            or FAT_UUID_RE.fullmatch(efi.filesystem_uuid.upper()) is None
            or efi.partition_type != EFI_SYSTEM_PARTITION_GUID
            or efi.mountpoints != ("/boot/efi",)
            or efi.children
            or boot.device_type != "part"
            or boot.logical_sector_size != OS_LOGICAL_SECTOR_BYTES
            or boot.start_sector != OS_BOOT_PARTITION_START_SECTOR
            or boot.size_bytes != OS_BOOT_PARTITION_BYTES
            or boot.filesystem != "ext4"
            or CANONICAL_UUID_RE.fullmatch(boot.filesystem_uuid) is None
            or boot.partition_type != LINUX_FILESYSTEM_PARTITION_GUID
            or boot.mountpoints != ("/boot",)
            or boot.children
            or lvm_partition.device_type != "part"
            or lvm_partition.logical_sector_size != OS_LOGICAL_SECTOR_BYTES
            or lvm_partition.start_sector != OS_LVM_PARTITION_START_SECTOR
            or lvm_partition.size_bytes < OS_LVM_PARTITION_BASELINE_BYTES
            or lvm_partition.filesystem != "LVM2_member"
            or LVM_IDENTIFIER_RE.fullmatch(lvm_partition.filesystem_uuid) is None
            or lvm_partition.partition_type != LINUX_FILESYSTEM_PARTITION_GUID
            or lvm_partition.mountpoints
            or lvm_partition_end > os_disk.size_bytes
            or os_disk.size_bytes - lvm_partition_end
            > OS_LAYOUT_TAIL_TOLERANCE_BYTES
            or root.path != OS_ROOT_LV_PATH
            or root.device_type != "lvm"
            or root.logical_sector_size != OS_LOGICAL_SECTOR_BYTES
            or root.size_bytes < OS_ROOT_LV_BASELINE_BYTES
            or root.size_bytes > lvm_partition.size_bytes
            or lvm_partition.size_bytes - root.size_bytes
            > OS_LAYOUT_TAIL_TOLERANCE_BYTES
            or root.filesystem != "ext4"
            or CANONICAL_UUID_RE.fullmatch(root.filesystem_uuid) is None
            or root.mountpoints != ("/",)
            or root.children
            or root.major_minor != root_major_minor
            or parents.get(root.major_minor) != lvm_partition.major_minor
            or parents.get(lvm_partition.major_minor) != os_disk.major_minor
        ):
            raise StorageInitializationError("os_disk_layout_invalid")
        capacity = self._root_filesystem_capacity()
        if (
            capacity < OS_ROOT_FILESYSTEM_MINIMUM_BYTES
            or capacity
            < root.size_bytes * OS_ROOT_FILESYSTEM_MINIMUM_LV_PERCENT // 100
            or capacity > root.size_bytes
        ):
            raise StorageInitializationError("root_filesystem_not_grown_to_lv")

    def _observe_devices(
        self,
        manifest: StorageManifest,
        *,
        require_blank: frozenset[DataRole],
        allow_expected_filesystems: frozenset[DataRole] = frozenset(),
        allow_device_growth: bool = False,
    ) -> dict[Role, DeviceObservation]:
        roots = self._lsblk()
        by_device, parents = _flatten_nodes(roots)
        root_major_minor = self._root_major_minor()
        protected = _ancestor_chain(root_major_minor, parents)
        if len([node for node in roots if node.device_type == "disk"]) != 5:
            raise StorageInitializationError("exactly_five_disks_required")
        observations: dict[Role, DeviceObservation] = {}
        selected_devices: set[str] = set()
        selected_serials: set[str] = set()
        for spec in ROLE_SPECS:
            requested = manifest.devices[spec.role]
            resolved, metadata = self._resolve_by_id(requested.by_id)
            major_minor = f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"
            node = by_device.get(major_minor)
            if node is None or node.path != resolved:
                raise StorageInitializationError("device_not_in_lsblk_snapshot")
            if spec.role != "os" and major_minor in protected:
                raise StorageInitializationError("system_device_selected_as_data")
            if (
                node.device_type != "disk"
                or node.read_only
                or node.removable
                or node.serial != requested.expected_serial
            ):
                raise StorageInitializationError("device_contract_mismatch")
            if spec.role != "os" and node.children:
                raise StorageInitializationError("data_device_has_children")
            expected_size = spec.nominal_bytes
            if spec.role == "os" or allow_device_growth:
                size_invalid = node.size_bytes < expected_size
            else:
                size_invalid = node.size_bytes != expected_size
            if size_invalid:
                raise StorageInitializationError("device_size_mismatch")
            if self._block_device_size(resolved) != node.size_bytes:
                raise StorageInitializationError("device_size_mismatch")
            if major_minor in selected_devices or node.serial in selected_serials:
                raise StorageInitializationError("device_identity_reused")
            selected_devices.add(major_minor)
            selected_serials.add(node.serial)
            if spec.role == "os" and major_minor not in protected:
                raise StorageInitializationError("os_device_does_not_back_root")
            self._assert_no_holders(major_minor)
            observation = DeviceObservation(
                role=spec.role,
                by_id=requested.by_id,
                resolved_path=resolved,
                size_bytes=node.size_bytes,
                serial=node.serial,
                wwn=node.wwn,
                major_minor=node.major_minor,
                filesystem=node.filesystem,
                label=node.label,
                filesystem_uuid=node.filesystem_uuid,
                mountpoints=node.mountpoints,
                identity_sha256=_safe_identity_digest(
                    by_id=requested.by_id,
                    node=node,
                ),
            )
            observations[spec.role] = observation
            if spec.role in require_blank:
                if (
                    node.filesystem
                    or node.label
                    or node.filesystem_uuid
                    or node.mountpoints
                ):
                    raise StorageInitializationError("device_not_blank")
                self._assert_blank_signatures(requested.by_id)
                self._assert_zero_samples(resolved, node.size_bytes)
            elif spec.role in allow_expected_filesystems:
                assert requested.filesystem_uuid is not None
                if (
                    node.filesystem != "xfs"
                    or node.label != spec.label
                    or node.filesystem_uuid != requested.filesystem_uuid
                ):
                    raise StorageInitializationError("planned_filesystem_mismatch")
        for role in DATA_ROLES:
            expected_uuid = manifest.devices[role].filesystem_uuid
            assert expected_uuid is not None
            matches = [
                node
                for node in by_device.values()
                if node.filesystem_uuid == expected_uuid
            ]
            if not matches:
                continue
            own = observations[role]
            if (
                role in require_blank
                or len(matches) != 1
                or matches[0].major_minor != own.major_minor
            ):
                raise StorageInitializationError("planned_filesystem_uuid_conflict")
        os_observation = observations["os"]
        self._assert_os_disk_layout(
            by_device[os_observation.major_minor],
            root_major_minor=root_major_minor,
            parents=parents,
        )
        if {node.major_minor for node in roots if node.device_type == "disk"} != selected_devices:
            raise StorageInitializationError("unapproved_disk_present")
        return observations

    def _build_plan(self, manifest: StorageManifest) -> LivePlan:
        self._require_control_path_contract()
        self._require_tools()
        self._require_real_vm_host()
        self._require_time_contract()
        self._require_services_stopped()
        self._require_root_filesystem_contract()
        observations = self._observe_devices(
            manifest,
            require_blank=frozenset(DATA_ROLES),
        )
        self._inspect_mountpoint_candidates()
        fstab, fstab_metadata, root_filesystem_uuid = self._inspect_fstab()
        self._assert_fstab_sources_disjoint_from_data(fstab, observations)
        host_identity = self._host_identity()
        devices: list[dict[str, object]] = []
        for spec in ROLE_SPECS:
            observation = observations[spec.role]
            payload = observation.plan_payload()
            if spec.role != "os":
                payload["filesystem_uuid"] = manifest.devices[spec.role].filesystem_uuid
            devices.append(payload)
        canonical: dict[str, object] = {
            "change_id": manifest.change_id,
            "devices": devices,
            "filesystem_layout": (
                "ubuntu_gpt_efi_boot_single_pv_single_root_lv_swapfile_"
                "plus_four_whole_device_xfs_data_disks"
            ),
            "fstab": {
                "inode": fstab_metadata.st_ino,
                "mode": f"{stat.S_IMODE(fstab_metadata.st_mode):04o}",
                "sha256": _sha256(fstab),
            },
            "host_identity": host_identity,
            "manifest_sha256": manifest.sha256,
            "not_after": manifest.not_after.isoformat(),
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "reviewer": manifest.reviewer,
            "root_filesystem_uuid": root_filesystem_uuid,
            "script_sha256": self._script_sha256(),
        }
        digest = _sha256(_canonical_json(canonical))
        hostname = os.uname().nodename
        safe_hostname = re.sub(r"[^A-Za-z0-9.-]", "-", hostname)[:63] or "host"
        return LivePlan(
            canonical=canonical,
            sha256=digest,
            observations=observations,
            confirmation_token=f"ERASE-4-DATA-DISKS-{safe_hostname}-{digest[:16]}",
        )

    def plan(self) -> dict[str, object]:
        """Execute read-only checks and return the exact live plan digest."""

        if os.path.lexists(self.intent_path):
            raise StorageInitializationError("unfinished_intent_exists")
        if os.path.lexists(self.state_path):
            raise StorageInitializationError("storage_already_initialized")
        return self._build_plan(self._read_manifest()).public_payload()

    def _require_control_path_contract(self) -> None:
        parent = self.state_path.parent
        if (
            not self.manifest_path.is_absolute()
            or not self.state_path.is_absolute()
            or not self.intent_path.is_absolute()
            or not self.lock_path.is_absolute()
            or parent != self.intent_path.parent
            or parent != self.manifest_path.parent
            or parent != self.lock_path.parent
        ):
            raise StorageInitializationError("control_paths_must_share_parent")

    def _ensure_control_directory(self) -> None:
        self._require_control_path_contract()
        parent = self.state_path.parent
        if not os.path.lexists(parent):
            grandparent = parent.parent
            _assert_path_chain(grandparent, allow_missing=False)
        try:
            _create_fixed_directory(parent, uid=0, gid=0, mode=0o700)
        except StorageInitializationError as exc:
            raise StorageInitializationError("control_directory_unsafe") from exc

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        parent = self.lock_path.parent
        _assert_path_chain(parent, allow_missing=False)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise StorageInitializationError("storage_lock_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise StorageInitializationError("storage_lock_unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StorageInitializationError("storage_lock_busy") from exc
            if self._active_lock_fd is not None:
                raise StorageInitializationError("storage_lock_state_invalid")
            self._active_lock_fd = descriptor
            yield
        finally:
            try:
                if self._active_lock_fd == descriptor:
                    self._active_lock_fd = None
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _write_intent(self, intent: Mapping[str, object]) -> None:
        _atomic_write(
            self.intent_path,
            _control_payload(intent),
            mode=0o600,
            uid=0,
            gid=0,
        )

    def _write_state(self, state: Mapping[str, object]) -> None:
        if os.path.lexists(self.state_path):
            raise StorageInitializationError("state_already_exists")
        _atomic_create(
            self.state_path,
            _control_payload(state),
            mode=0o600,
            uid=0,
            gid=0,
        )

    def _read_control(self, path: Path) -> dict[str, object]:
        payload, _metadata = _read_regular_secure(
            path,
            maximum_bytes=MAX_CONTROL_FILE_BYTES,
            expected_uid=0,
            modes=frozenset({0o600}),
        )
        try:
            raw = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StorageInitializationError("control_file_invalid") from exc
        if not isinstance(raw, dict):
            raise StorageInitializationError("control_file_invalid")
        return cast(dict[str, object], raw)

    def _new_intent(self, plan: LivePlan) -> dict[str, object]:
        return {
            "current_role": None,
            "directories_complete": False,
            "formatted": [],
            "fstab": None,
            "intent_schema_version": INTENT_SCHEMA_VERSION,
            "mounted": [],
            "phase": "prepared",
            "plan": plan.canonical,
            "plan_sha256": plan.sha256,
        }

    def _validate_plan_object(self, value: object, digest: object) -> Mapping[str, object]:
        if (
            not isinstance(value, dict)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or _sha256(_canonical_json(value)) != digest
            or value.get("plan_schema_version") != PLAN_SCHEMA_VERSION
            or not isinstance(value.get("devices"), list)
            or len(cast(list[object], value["devices"])) != len(ROLE_SPECS)
        ):
            raise StorageInitializationError("stored_plan_invalid")
        return cast(Mapping[str, object], value)

    def _load_intent(self) -> dict[str, object]:
        intent = self._read_control(self.intent_path)
        expected_keys = {
            "current_role",
            "directories_complete",
            "formatted",
            "fstab",
            "intent_schema_version",
            "mounted",
            "phase",
            "plan",
            "plan_sha256",
        }
        if (
            set(intent) != expected_keys
            or intent.get("intent_schema_version") != INTENT_SCHEMA_VERSION
        ):
            raise StorageInitializationError("intent_invalid")
        self._validate_plan_object(intent.get("plan"), intent.get("plan_sha256"))
        phase = intent.get("phase")
        current_role = intent.get("current_role")
        formatted = intent.get("formatted")
        mounted = intent.get("mounted")
        if phase not in {
            "prepared",
            "formatting",
            "formatted",
            "fstab_prepared",
            "fstab_written",
            "mounting",
            "directories",
            "verifying",
        }:
            raise StorageInitializationError("intent_invalid")
        if current_role is not None and current_role not in DATA_ROLES:
            raise StorageInitializationError("intent_invalid")
        if (
            not isinstance(formatted, list)
            or any(role not in DATA_ROLES for role in formatted)
            or len(set(cast(list[str], formatted))) != len(formatted)
            or not isinstance(mounted, list)
            or any(role not in DATA_ROLES for role in mounted)
            or len(set(cast(list[str], mounted))) != len(mounted)
            or not isinstance(intent.get("directories_complete"), bool)
        ):
            raise StorageInitializationError("intent_invalid")
        if cast(list[str], formatted) != list(DATA_ROLES[: len(formatted)]):
            raise StorageInitializationError("intent_invalid")
        if cast(list[str], mounted) != list(DATA_ROLES[: len(mounted)]):
            raise StorageInitializationError("intent_invalid")
        raw_fstab = intent.get("fstab")
        if raw_fstab is not None:
            if not isinstance(raw_fstab, dict) or set(raw_fstab) != {
                "backup_path",
                "expected_sha256",
                "original_inode",
                "original_sha256",
            }:
                raise StorageInitializationError("intent_invalid")
            backup_path = raw_fstab.get("backup_path")
            if (
                not isinstance(backup_path, str)
                or Path(backup_path).parent != self.fstab_path.parent
                or not Path(backup_path).name.startswith("fstab.sms-platform.")
                or any(
                    not isinstance(raw_fstab.get(key), str)
                    or re.fullmatch(r"[0-9a-f]{64}", cast(str, raw_fstab[key])) is None
                    for key in ("expected_sha256", "original_sha256")
                )
                or not isinstance(raw_fstab.get("original_inode"), int)
            ):
                raise StorageInitializationError("intent_invalid")
        return intent

    def _clear_intent(self) -> None:
        try:
            self.intent_path.unlink()
            _fsync_directory(self.intent_path.parent)
        except OSError as exc:
            raise StorageInitializationError("intent_clear_failed") from exc

    def _manifest_from_plan(self, plan: Mapping[str, object]) -> StorageManifest:
        raw_devices = plan.get("devices")
        if not isinstance(raw_devices, list):
            raise StorageInitializationError("stored_plan_invalid")
        devices: dict[Role, ManifestDevice] = {}
        for expected_spec, raw_device in zip(ROLE_SPECS, raw_devices, strict=True):
            if not isinstance(raw_device, dict) or raw_device.get("role") != expected_spec.role:
                raise StorageInitializationError("stored_plan_invalid")
            try:
                by_id = _safe_by_id(raw_device["by_id"])
                serial = raw_device["serial"]
            except KeyError as exc:
                raise StorageInitializationError("stored_plan_invalid") from exc
            if not isinstance(serial, str) or SAFE_IDENTITY_RE.fullmatch(serial) is None:
                raise StorageInitializationError("stored_plan_invalid")
            filesystem_uuid = None
            if expected_spec.role != "os":
                filesystem_uuid = _parse_canonical_uuid(raw_device.get("filesystem_uuid"))
            devices[expected_spec.role] = ManifestDevice(by_id, serial, filesystem_uuid)
        expiry = plan.get("not_after")
        if not isinstance(expiry, str):
            raise StorageInitializationError("stored_plan_invalid")
        try:
            parsed_expiry = datetime.fromisoformat(expiry)
        except ValueError as exc:
            raise StorageInitializationError("stored_plan_invalid") from exc
        change_id = plan.get("change_id")
        reviewer = plan.get("reviewer")
        manifest_sha256 = plan.get("manifest_sha256")
        if (
            not isinstance(change_id, str)
            or not isinstance(reviewer, str)
            or not isinstance(manifest_sha256, str)
        ):
            raise StorageInitializationError("stored_plan_invalid")
        return StorageManifest(
            change_id,
            reviewer,
            parsed_expiry,
            devices,
            manifest_sha256,
        )

    def _assert_stable_plan_devices(
        self,
        plan: Mapping[str, object],
        observations: Mapping[Role, DeviceObservation],
        *,
        allow_device_growth: bool = False,
    ) -> None:
        raw_devices = plan.get("devices")
        if not isinstance(raw_devices, list):
            raise StorageInitializationError("stored_plan_invalid")
        for spec, raw in zip(ROLE_SPECS, raw_devices, strict=True):
            if not isinstance(raw, dict):
                raise StorageInitializationError("stored_plan_invalid")
            observation = observations[spec.role]
            if (
                raw.get("role") != spec.role
                or raw.get("by_id") != str(observation.by_id)
                or raw.get("serial") != observation.serial
                or raw.get("nominal_size_bytes") != spec.nominal_bytes
                or raw.get("identity_sha256") != observation.identity_sha256
                or (
                    observation.size_bytes < spec.nominal_bytes
                    if spec.role == "os" or allow_device_growth
                    else observation.size_bytes != spec.nominal_bytes
                )
            ):
                raise StorageInitializationError("planned_device_identity_changed")

    def _assert_recovery_context(self, plan: Mapping[str, object]) -> None:
        stored_host = plan.get("host_identity")
        if (
            plan.get("script_sha256") != self._script_sha256()
            or not isinstance(stored_host, dict)
            or set(stored_host)
            != {"boot_id", "machine_id_sha256", "product_uuid_sha256"}
            or any(not isinstance(value, str) for value in stored_host.values())
        ):
            raise StorageInitializationError("recovery_context_invalid")
        current_host = self._host_identity()
        if (
            stored_host.get("machine_id_sha256")
            != current_host["machine_id_sha256"]
            or stored_host.get("product_uuid_sha256")
            != current_host["product_uuid_sha256"]
        ):
            raise StorageInitializationError("recovery_host_changed")

    def _verify_expected_filesystem(
        self,
        role: DataRole,
        path: Path,
        *,
        pass_fds: Sequence[int] = (),
    ) -> None:
        spec = SPECS_BY_ROLE[role]
        manifest = self._active_manifest
        expected_uuid = manifest.devices[role].filesystem_uuid
        assert expected_uuid is not None and spec.label is not None
        result = self._run(
            (BLKID_BINARY, "-p", "-o", "export", str(path)),
            code="planned_filesystem_probe_failed",
            pass_fds=pass_fds,
        )
        values: dict[str, str] = {}
        try:
            text = result.stdout.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise StorageInitializationError("planned_filesystem_probe_invalid") from exc
        for raw_line in text.splitlines():
            key, separator, value = raw_line.partition("=")
            if not separator or key in values or not re.fullmatch(r"[A-Z0-9_]+", key):
                raise StorageInitializationError("planned_filesystem_probe_invalid")
            values[key] = value
        if (
            values.get("TYPE") != "xfs"
            or values.get("UUID", "").lower() != expected_uuid
            or values.get("LABEL") != spec.label
        ):
            raise StorageInitializationError("planned_filesystem_mismatch")

    def _format_role(self, role: DataRole, observation: DeviceObservation) -> None:
        spec = SPECS_BY_ROLE[role]
        expected_uuid = self._active_manifest.devices[role].filesystem_uuid
        assert expected_uuid is not None and spec.label is not None
        self._run(
            (UDEVADM_BINARY, "settle", "--timeout=30"),
            code="udev_settle_failed",
        )
        with self._open_revalidated_blank_device(observation) as descriptor:
            lock_descriptor = self._active_lock_fd
            if lock_descriptor is None:
                raise StorageInitializationError("destructive_lock_required")
            inherited = (descriptor, lock_descriptor)
            descriptor_path = Path(f"/proc/self/fd/{descriptor}")
            self._run(
                (
                    MKFS_XFS_BINARY,
                    "-q",
                    "-L",
                    spec.label,
                    "-m",
                    f"uuid={expected_uuid}",
                    "-n",
                    "ftype=1",
                    str(descriptor_path),
                ),
                code="mkfs_xfs_failed",
                timeout_seconds=MKFS_TIMEOUT_SECONDS,
                pass_fds=inherited,
            )
            self._verify_expected_filesystem(
                role,
                descriptor_path,
                pass_fds=inherited,
            )
            self._verify_xfs_ftype(descriptor_path, pass_fds=inherited)
        self._run(
            (UDEVADM_BINARY, "settle", "--timeout=30"),
            code="udev_settle_failed",
        )
        self._verify_expected_filesystem(role, observation.by_id)

    def _phase_observations(
        self,
        intent: Mapping[str, object],
        *,
        current_role_may_be_complete: bool,
    ) -> tuple[dict[Role, DeviceObservation], bool]:
        plan = cast(Mapping[str, object], intent["plan"])
        self._require_services_stopped()
        self._assert_fstab_matches_plan(plan)
        self._inspect_mountpoint_candidates()
        manifest = self._manifest_from_plan(plan)
        self._active_manifest = manifest
        formatted = cast(list[DataRole], intent["formatted"])
        current = cast(DataRole | None, intent["current_role"])
        remaining = [role for role in DATA_ROLES if role not in formatted and role != current]
        observations = self._observe_devices(
            manifest,
            require_blank=frozenset(remaining),
            allow_expected_filesystems=frozenset(formatted),
        )
        for formatted_role in formatted:
            self._verify_xfs_ftype(observations[formatted_role].by_id)
        current_complete = False
        if current is not None:
            observation = observations[current]
            expected_uuid = manifest.devices[current].filesystem_uuid
            spec = SPECS_BY_ROLE[current]
            if (
                observation.filesystem == "xfs"
                and observation.label == spec.label
                and observation.filesystem_uuid == expected_uuid
            ):
                if not current_role_may_be_complete:
                    raise StorageInitializationError("unexpected_planned_filesystem")
                self._verify_expected_filesystem(current, observation.by_id)
                self._verify_xfs_ftype(observation.by_id)
                current_complete = True
            elif (
                not observation.filesystem
                and not observation.label
                and not observation.filesystem_uuid
                and not observation.mountpoints
            ):
                self._assert_blank_signatures(observation.by_id)
                self._assert_zero_samples(observation.resolved_path, observation.size_bytes)
            else:
                raise StorageInitializationError("unsafe_partial_filesystem")
        self._assert_stable_plan_devices(plan, observations)
        return observations, current_complete

    def _prepare_fstab_intent(
        self,
        intent: dict[str, object],
    ) -> tuple[dict[str, object], bytes, bytes]:
        original, metadata = _read_regular_secure(
            self.fstab_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600, 0o644}),
        )
        self._assert_fstab_matches_plan(cast(Mapping[str, object], intent["plan"]))
        plan_sha256 = cast(str, intent["plan_sha256"])
        expected = render_fstab(original, self._active_manifest, plan_sha256)
        backup_path = self.fstab_path.parent / f"fstab.sms-platform.{plan_sha256[:16]}.bak"
        updated = {
            **intent,
            "fstab": {
                "backup_path": str(backup_path),
                "expected_sha256": _sha256(expected),
                "original_inode": metadata.st_ino,
                "original_sha256": _sha256(original),
            },
            "phase": "fstab_prepared",
        }
        self._write_intent(updated)
        return updated, original, expected

    def _secure_backup(self, path: Path, expected_payload: bytes) -> None:
        if os.path.lexists(path):
            _fsync_existing_regular_secure(
                path,
                expected_payload=expected_payload,
                maximum_bytes=MAX_FSTAB_BYTES,
                expected_uid=0,
                modes=frozenset({0o600}),
                mismatch_code="fstab_backup_mismatch",
            )
            return
        _atomic_create(path, expected_payload, mode=0o600, uid=0, gid=0)

    def _replace_fstab(
        self,
        *,
        original: bytes,
        expected: bytes,
        original_inode: int,
    ) -> None:
        _assert_fstab_target_paths_safe(expected)
        current, metadata = _read_regular_secure(
            self.fstab_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600, 0o644}),
        )
        if current == expected:
            _fsync_existing_regular_secure(
                self.fstab_path,
                expected_payload=expected,
                maximum_bytes=MAX_FSTAB_BYTES,
                expected_uid=0,
                modes=frozenset({0o600, 0o644}),
                mismatch_code="fstab_changed_since_plan",
            )
            return
        if current != original or metadata.st_ino != original_inode:
            raise StorageInitializationError("fstab_changed_since_plan")
        temporary = self.fstab_path.parent / f".{self.fstab_path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(metadata.st_mode),
        )
        try:
            os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
            os.fchown(descriptor, 0, 0)
            offset = 0
            while offset < len(expected):
                offset += os.write(descriptor, expected[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            self._verify_fstab_file(
                temporary,
                code="fstab_verification_failed",
            )
            reread, reread_metadata = _read_regular_secure(
                self.fstab_path,
                maximum_bytes=MAX_FSTAB_BYTES,
                expected_uid=0,
                modes=frozenset({0o600, 0o644}),
            )
            if reread != original or reread_metadata.st_ino != original_inode:
                raise StorageInitializationError("fstab_changed_since_plan")
            os.replace(temporary, self.fstab_path)
            _fsync_directory(self.fstab_path.parent)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def _ensure_mountpoints(
        self,
        observations: Mapping[Role, DeviceObservation],
    ) -> None:
        sms_root = SMS_STORAGE_ROOT
        if not os.path.lexists(sms_root):
            _create_fixed_directory(sms_root, uid=0, gid=0, mode=0o750)
        else:
            _create_fixed_directory(sms_root, uid=0, gid=0, mode=0o750)
        for spec in DATA_SPECS:
            try:
                record = self._findmnt_record(spec.mount_path)
            except StorageInitializationError as exc:
                if exc.code != "mount_readback_failed":
                    raise
            else:
                if record.get("target") == str(spec.mount_path):
                    self._verify_mount(
                        cast(DataRole, spec.role), observations[spec.role]
                    )
                    continue
            if not os.path.lexists(spec.mount_path):
                _create_fixed_directory(
                    spec.mount_path,
                    uid=spec.mount_uid,
                    gid=spec.mount_gid,
                    mode=spec.mount_mode,
                )
            else:
                _create_fixed_directory(
                    spec.mount_path,
                    uid=spec.mount_uid,
                    gid=spec.mount_gid,
                    mode=spec.mount_mode,
                )
                if not _directory_is_empty(spec.mount_path):
                    raise StorageInitializationError("mountpoint_not_empty")

    def _findmnt_record(self, target: Path) -> dict[str, object]:
        result = self._run(
            (
                FINDMNT_BINARY,
                "--json",
                "--bytes",
                "--output",
                "SOURCE,TARGET,FSTYPE,OPTIONS,FSROOT,MAJ:MIN,SIZE",
                "--target",
                str(target),
            ),
            code="mount_readback_failed",
        )
        try:
            raw = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StorageInitializationError("mount_readback_invalid") from exc
        if not isinstance(raw, dict) or set(raw) != {"filesystems"}:
            raise StorageInitializationError("mount_readback_invalid")
        filesystems = raw["filesystems"]
        if not isinstance(filesystems, list) or len(filesystems) != 1:
            raise StorageInitializationError("mount_readback_invalid")
        item = filesystems[0]
        if not isinstance(item, dict):
            raise StorageInitializationError("mount_readback_invalid")
        return cast(dict[str, object], item)

    def _verify_mount(
        self,
        role: DataRole,
        observation: DeviceObservation,
    ) -> None:
        spec = SPECS_BY_ROLE[role]
        record = self._findmnt_record(spec.mount_path)
        options = record.get("options")
        size = record.get("size")
        if isinstance(size, str) and re.fullmatch(r"[0-9]+", size):
            size_value = int(size)
        elif isinstance(size, int) and not isinstance(size, bool):
            size_value = size
        else:
            raise StorageInitializationError("mount_readback_invalid")
        if (
            record.get("target") != str(spec.mount_path)
            or record.get("fstype") != "xfs"
            or record.get("fsroot") != "/"
            or record.get("maj:min") != observation.major_minor
            or not isinstance(options, str)
            or not {"rw", "nodev", "nosuid"} <= set(options.split(","))
            or "ro" in set(options.split(","))
            or size_value < observation.size_bytes * 98 // 100
            or size_value > observation.size_bytes
        ):
            raise StorageInitializationError("mounted_filesystem_contract_mismatch")

    def _mount_role(self, role: DataRole, observation: DeviceObservation) -> None:
        spec = SPECS_BY_ROLE[role]
        try:
            existing = self._findmnt_record(spec.mount_path)
        except StorageInitializationError as exc:
            if exc.code != "mount_readback_failed":
                raise
        else:
            if existing.get("target") == str(spec.mount_path):
                self._verify_mount(role, observation)
                return
        self._run(
            (MOUNT_BINARY, "--", str(spec.mount_path)),
            code="mount_failed",
        )
        self._verify_mount(role, observation)

    def _set_mount_permissions_and_directories(
        self,
        observations: Mapping[Role, DeviceObservation],
    ) -> None:
        for spec in DATA_SPECS:
            observation = observations[spec.role]
            self._verify_mount(cast(DataRole, spec.role), observation)
            try:
                os.chown(spec.mount_path, spec.mount_uid, spec.mount_gid, follow_symlinks=False)
                os.chmod(spec.mount_path, spec.mount_mode, follow_symlinks=False)
                _fsync_directory(spec.mount_path)
            except OSError as exc:
                raise StorageInitializationError("mountpoint_permission_update_failed") from exc
            for directory in spec.directories:
                if not os.path.lexists(directory.path):
                    _create_fixed_directory(
                        directory.path,
                        uid=directory.uid,
                        gid=directory.gid,
                        mode=directory.mode,
                    )
                else:
                    metadata = directory.path.lstat()
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or stat.S_ISLNK(metadata.st_mode)
                        or metadata.st_dev != spec.mount_path.lstat().st_dev
                        or not _directory_is_empty(directory.path)
                    ):
                        raise StorageInitializationError("existing_data_directory_unsafe")
                    try:
                        os.chown(
                            directory.path,
                            directory.uid,
                            directory.gid,
                            follow_symlinks=False,
                        )
                        os.chmod(
                            directory.path,
                            directory.mode,
                            follow_symlinks=False,
                        )
                        _fsync_directory(directory.path)
                        _fsync_directory(directory.path.parent)
                    except OSError as exc:
                        raise StorageInitializationError(
                            "directory_permission_update_failed"
                        ) from exc
                metadata = directory.path.lstat()
                mount_metadata = spec.mount_path.lstat()
                if metadata.st_dev != mount_metadata.st_dev:
                    raise StorageInitializationError("directory_crosses_filesystem")

    def _verify_xfs_ftype(
        self,
        path: Path,
        *,
        pass_fds: Sequence[int] = (),
    ) -> None:
        result = self._run(
            (XFS_INFO_BINARY, str(path)),
            code="xfs_info_failed",
            pass_fds=pass_fds,
        )
        try:
            output = result.stdout.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise StorageInitializationError("xfs_info_invalid") from exc
        values = re.findall(
            rb"(?:^|[,\s])ftype=([01])(?=$|[,\s])",
            result.stdout,
        )
        if values != [b"1"] or "ftype=1" not in output:
            raise StorageInitializationError("xfs_ftype_required")

    def _verify_all_xfs_ftype(self) -> None:
        for spec in DATA_SPECS:
            self._verify_xfs_ftype(spec.mount_path)

    def _assert_exact_data_mountpoints(
        self,
        observations: Mapping[Role, DeviceObservation],
        *,
        allow_runtime_docker_binds: bool = False,
    ) -> None:
        approved_by_role: dict[DataRole, dict[str, DockerBindMountSpec]] = {
            role: {} for role in DATA_ROLES
        }
        for bind_spec in DOCKER_BIND_MOUNT_SPECS:
            approved_by_role[bind_spec.role][str(bind_spec.target_path)] = bind_spec
        for spec in DATA_SPECS:
            role = cast(DataRole, spec.role)
            observation = observations[role]
            actual = observation.mountpoints
            main_mount = str(spec.mount_path)
            if len(actual) != len(set(actual)) or main_mount not in actual:
                raise StorageInitializationError("unexpected_data_device_mountpoint")
            extras = set(actual) - {main_mount}
            if not allow_runtime_docker_binds:
                if extras:
                    raise StorageInitializationError(
                        "unexpected_data_device_mountpoint"
                    )
                continue
            approved = approved_by_role[role]
            if not extras <= set(approved):
                raise StorageInitializationError("unexpected_data_device_mountpoint")
            for target in sorted(extras):
                self._verify_runtime_docker_bind_mount(
                    approved[target],
                    observation,
                )

    def _verify_runtime_docker_bind_mount(
        self,
        bind_spec: DockerBindMountSpec,
        observation: DeviceObservation,
    ) -> None:
        try:
            metadata = bind_spec.target_path.lstat()
            canonical = bind_spec.target_path.resolve(strict=True)
        except OSError as exc:
            raise StorageInitializationError(
                "docker_bind_mount_contract_mismatch"
            ) from exc
        record = self._findmnt_record(bind_spec.target_path)
        options = record.get("options")
        if (
            canonical != bind_spec.target_path
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
            != observation.major_minor
            or record.get("target") != str(bind_spec.target_path)
            or record.get("fstype") != "xfs"
            or record.get("fsroot") != str(bind_spec.filesystem_root)
            or record.get("maj:min") != observation.major_minor
            or not isinstance(options, str)
            or not {"rw", "nodev", "nosuid"} <= set(options.split(","))
            or "ro" in set(options.split(","))
        ):
            raise StorageInitializationError(
                "docker_bind_mount_contract_mismatch"
            )

    def _verify_final_layout(
        self,
        plan: Mapping[str, object],
        *,
        allow_device_growth: bool = False,
        allow_runtime_docker_binds: bool = False,
    ) -> dict[Role, DeviceObservation]:
        self._assert_storage_parent_contract(allow_missing=False)
        if plan.get("root_filesystem_uuid") != self._root_filesystem_uuid():
            raise StorageInitializationError("root_filesystem_uuid_changed")
        manifest = self._manifest_from_plan(plan)
        self._active_manifest = manifest
        observations = self._observe_devices(
            manifest,
            require_blank=frozenset(),
            allow_expected_filesystems=frozenset(DATA_ROLES),
            allow_device_growth=allow_device_growth,
        )
        self._assert_stable_plan_devices(
            plan,
            observations,
            allow_device_growth=allow_device_growth,
        )
        plan_digest = _sha256(_canonical_json(plan))
        backup_path = self.fstab_path.parent / f"fstab.sms-platform.{plan_digest[:16]}.bak"
        original, _ = _read_regular_secure(
            backup_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600}),
        )
        plan_fstab = plan.get("fstab")
        if (
            not isinstance(plan_fstab, dict)
            or plan_fstab.get("sha256") != _sha256(original)
        ):
            raise StorageInitializationError("fstab_backup_mismatch")
        current, _ = _read_regular_secure(
            self.fstab_path,
            maximum_bytes=MAX_FSTAB_BYTES,
            expected_uid=0,
            modes=frozenset({0o600, 0o644}),
        )
        _assert_fstab_target_paths_safe(current)
        if current != render_fstab(original, manifest, plan_digest):
            raise StorageInitializationError("fstab_live_state_mismatch")
        self._assert_exact_data_mountpoints(
            observations,
            allow_runtime_docker_binds=allow_runtime_docker_binds,
        )
        for spec in DATA_SPECS:
            self._verify_expected_filesystem(
                cast(DataRole, spec.role), observations[spec.role].by_id
            )
            self._verify_mount(cast(DataRole, spec.role), observations[spec.role])
            mount_metadata = spec.mount_path.lstat()
            if (
                not stat.S_ISDIR(mount_metadata.st_mode)
                or stat.S_ISLNK(mount_metadata.st_mode)
                or mount_metadata.st_uid != spec.mount_uid
                or mount_metadata.st_gid != spec.mount_gid
                or stat.S_IMODE(mount_metadata.st_mode) != spec.mount_mode
            ):
                raise StorageInitializationError("mountpoint_contract_mismatch")
            for directory in spec.directories:
                metadata = directory.path.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != directory.uid
                    or metadata.st_gid != directory.gid
                    or stat.S_IMODE(metadata.st_mode) != directory.mode
                    or metadata.st_dev != mount_metadata.st_dev
                ):
                    raise StorageInitializationError("directory_contract_mismatch")
        self._verify_all_xfs_ftype()
        return observations

    def _continue(self, intent: dict[str, object]) -> dict[str, object]:
        plan = cast(Mapping[str, object], intent["plan"])
        formatted = cast(list[DataRole], intent["formatted"])
        current_role = cast(DataRole | None, intent["current_role"])
        if not formatted and current_role is None and intent.get("fstab") is None:
            initial_observations, _complete = self._phase_observations(
                intent,
                current_role_may_be_complete=False,
            )
            self._ensure_mountpoints(initial_observations)
        if current_role is not None:
            observations, complete = self._phase_observations(
                intent,
                current_role_may_be_complete=True,
            )
            if not complete:
                self._format_role(current_role, observations[current_role])
            formatted.append(current_role)
            intent = {
                **intent,
                "current_role": None,
                "formatted": formatted,
                "phase": "formatted",
            }
            self._write_intent(intent)

        for role in DATA_ROLES[len(formatted) :]:
            intent = {**intent, "current_role": role, "phase": "formatting"}
            self._write_intent(intent)
            observations, complete = self._phase_observations(
                intent,
                current_role_may_be_complete=True,
            )
            if not complete:
                self._format_role(role, observations[role])
            formatted = [*formatted, role]
            intent = {
                **intent,
                "current_role": None,
                "formatted": formatted,
                "phase": "formatted",
            }
            self._write_intent(intent)

        all_formatted = self._observe_devices(
            self._active_manifest,
            require_blank=frozenset(),
            allow_expected_filesystems=frozenset(DATA_ROLES),
        )
        self._assert_stable_plan_devices(plan, all_formatted)
        for role in DATA_ROLES:
            self._verify_xfs_ftype(all_formatted[role].by_id)
        fstab_info = intent.get("fstab")
        if fstab_info is None:
            self._inspect_mountpoint_candidates()
            intent, original, expected = self._prepare_fstab_intent(intent)
            fstab_info = cast(dict[str, object], intent["fstab"])
        else:
            if not isinstance(fstab_info, dict):
                raise StorageInitializationError("intent_invalid")
            backup_path = Path(cast(str, fstab_info["backup_path"]))
            original, _ = _read_regular_secure(
                backup_path,
                maximum_bytes=MAX_FSTAB_BYTES,
                expected_uid=0,
                modes=frozenset({0o600}),
            ) if os.path.lexists(backup_path) else _read_regular_secure(
                self.fstab_path,
                maximum_bytes=MAX_FSTAB_BYTES,
                expected_uid=0,
                modes=frozenset({0o600, 0o644}),
            )
            expected = render_fstab(
                original,
                self._active_manifest,
                cast(str, intent["plan_sha256"]),
            )
            if (
                _sha256(original) != fstab_info.get("original_sha256")
                or _sha256(expected) != fstab_info.get("expected_sha256")
            ):
                raise StorageInitializationError("fstab_intent_mismatch")

        assert isinstance(fstab_info, dict)
        backup_path = Path(cast(str, fstab_info["backup_path"]))
        self._secure_backup(backup_path, original)
        self._replace_fstab(
            original=original,
            expected=expected,
            original_inode=cast(int, fstab_info["original_inode"]),
        )
        intent = {**intent, "phase": "fstab_written"}
        self._write_intent(intent)

        observations = self._observe_devices(
            self._active_manifest,
            require_blank=frozenset(),
            allow_expected_filesystems=frozenset(DATA_ROLES),
        )
        self._assert_stable_plan_devices(plan, observations)
        self._ensure_mountpoints(observations)
        mounted = cast(list[DataRole], intent["mounted"])
        for role in DATA_ROLES:
            intent = {**intent, "phase": "mounting"}
            self._write_intent(intent)
            self._mount_role(role, observations[role])
            if role not in mounted:
                mounted = [*mounted, role]
            intent = {**intent, "mounted": mounted, "phase": "mounting"}
            self._write_intent(intent)

        intent = {**intent, "phase": "directories"}
        self._write_intent(intent)
        self._set_mount_permissions_and_directories(observations)
        intent = {**intent, "directories_complete": True, "phase": "verifying"}
        self._write_intent(intent)
        self._verify_final_layout(plan)
        state = {
            "plan": plan,
            "plan_sha256": intent["plan_sha256"],
            "state_schema_version": STATE_SCHEMA_VERSION,
            "status": "initialized",
        }
        self._write_state(state)
        self._clear_intent()
        return {
            "action": "apply",
            "change_id": plan["change_id"],
            "plan_sha256": intent["plan_sha256"],
            "status": "initialized",
        }

    def apply(self, expected_plan_sha256: str) -> dict[str, object]:
        """Apply a freshly revalidated plan after controlling-TTY confirmations."""

        if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
            raise StorageInitializationError("plan_sha256_invalid")
        if os.path.lexists(self.state_path):
            status = self.status()
            if status.get("status") == "initialized":
                if status.get("plan_sha256") != expected_plan_sha256:
                    raise StorageInitializationError("plan_sha256_mismatch")
                return {
                    "action": "apply",
                    "plan_sha256": status["plan_sha256"],
                    "status": "already_initialized",
                }
            raise StorageInitializationError("existing_state_not_verified")
        if os.path.lexists(self.intent_path):
            raise StorageInitializationError("resume_required")
        manifest = self._read_manifest()
        plan = self._build_plan(manifest)
        if plan.sha256 != expected_plan_sha256:
            raise StorageInitializationError("plan_sha256_mismatch")
        self.confirmation_reader.confirm(plan)
        self._ensure_control_directory()
        with self._exclusive_lock():
            if os.path.lexists(self.intent_path) or os.path.lexists(self.state_path):
                raise StorageInitializationError("storage_state_changed")
            manifest = self._read_manifest()
            plan = self._build_plan(manifest)
            if plan.sha256 != expected_plan_sha256:
                raise StorageInitializationError("plan_changed_before_apply")
            self._active_manifest = manifest
            intent = self._new_intent(plan)
            self._write_intent(intent)
            return self._continue(intent)

    def resume(self, expected_plan_sha256: str) -> dict[str, object]:
        """Continue only the exact durable intent; never adopt an unrelated filesystem."""

        if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
            raise StorageInitializationError("plan_sha256_invalid")
        if os.path.lexists(self.state_path) and os.path.lexists(self.intent_path):
            status = self.status()
            if (
                status.get("status") != "finalization_required"
                or status.get("plan_sha256") != expected_plan_sha256
            ):
                raise StorageInitializationError("finalization_state_not_verified")
            self._require_services_stopped()
            state = self._read_control(self.state_path)
            plan = self._validate_state(state)
            self._assert_recovery_context(plan)
            manifest = self._manifest_from_plan(plan)
            self._active_manifest = manifest
            observations = self._observe_devices(
                manifest,
                require_blank=frozenset(),
                allow_expected_filesystems=frozenset(DATA_ROLES),
            )
            token_host = re.sub(
                r"[^A-Za-z0-9.-]", "-", os.uname().nodename
            )[:63]
            confirmation_plan = LivePlan(
                canonical=plan,
                sha256=expected_plan_sha256,
                observations=observations,
                confirmation_token=(
                    f"FINALIZE-4-DATA-DISKS-{token_host}-{expected_plan_sha256[:16]}"
                ),
            )
            self.confirmation_reader.confirm(confirmation_plan)
            with self._exclusive_lock():
                self._assert_recovery_context(plan)
                self._verify_final_layout(plan)
                self._clear_intent()
            return {
                "action": "resume",
                "change_id": plan["change_id"],
                "plan_sha256": expected_plan_sha256,
                "status": "initialized",
            }
        if not os.path.lexists(self.intent_path):
            if os.path.lexists(self.state_path):
                status = self.status()
                if status.get("status") == "initialized":
                    if status.get("plan_sha256") != expected_plan_sha256:
                        raise StorageInitializationError("plan_sha256_mismatch")
                    return {
                        "action": "resume",
                        "plan_sha256": status["plan_sha256"],
                        "status": "already_initialized",
                    }
            raise StorageInitializationError("intent_missing")
        self._require_tools()
        self._require_real_vm_host()
        self._require_services_stopped()
        self._require_root_filesystem_contract()
        intent = self._load_intent()
        if intent["plan_sha256"] != expected_plan_sha256:
            raise StorageInitializationError("plan_sha256_mismatch")
        plan = cast(Mapping[str, object], intent["plan"])
        self._assert_recovery_context(plan)
        manifest = self._read_manifest(allow_expired_recovery=True)
        if manifest.sha256 != plan.get("manifest_sha256"):
            raise StorageInitializationError("manifest_changed_during_resume")
        stored_manifest = self._manifest_from_plan(plan)
        self._active_manifest = stored_manifest
        confirmation_plan = LivePlan(
            canonical=plan,
            sha256=expected_plan_sha256,
            observations=self._observe_devices(
                stored_manifest,
                require_blank=frozenset(),
            ),
            confirmation_token=(
                f"RESUME-4-DATA-DISKS-"
                f"{re.sub(r'[^A-Za-z0-9.-]', '-', os.uname().nodename)[:63]}-"
                f"{expected_plan_sha256[:16]}"
            ),
        )
        self.confirmation_reader.confirm(confirmation_plan)
        self._ensure_control_directory()
        with self._exclusive_lock():
            intent = self._load_intent()
            if intent["plan_sha256"] != expected_plan_sha256:
                raise StorageInitializationError("intent_changed_before_resume")
            plan = cast(Mapping[str, object], intent["plan"])
            self._assert_recovery_context(plan)
            result = self._continue(intent)
            return {**result, "action": "resume"}

    def _validate_state(self, state: Mapping[str, object]) -> Mapping[str, object]:
        if set(state) != {"plan", "plan_sha256", "state_schema_version", "status"}:
            raise StorageInitializationError("state_invalid")
        if (
            state.get("state_schema_version") != STATE_SCHEMA_VERSION
            or state.get("status") != "initialized"
        ):
            raise StorageInitializationError("state_invalid")
        return self._validate_plan_object(state.get("plan"), state.get("plan_sha256"))

    def status(self) -> dict[str, object]:
        """Read back all live evidence; never create a lock, directory, mount or file."""

        intent_present = os.path.lexists(self.intent_path)
        state_present = os.path.lexists(self.state_path)
        if not state_present:
            if not intent_present:
                return {"action": "status", "status": "absent"}
            try:
                intent = self._load_intent()
            except StorageInitializationError:
                return {"action": "status", "status": "unsafe_partial"}
            return {
                "action": "status",
                "current_role": intent["current_role"],
                "phase": intent["phase"],
                "plan_sha256": intent["plan_sha256"],
                "status": "in_progress",
            }
        try:
            self._require_tools()
            self._require_real_vm_host()
            self._require_root_filesystem_contract()
            state = self._read_control(self.state_path)
            plan = self._validate_state(state)
            self._assert_recovery_context(plan)
            if intent_present:
                intent = self._load_intent()
                if intent.get("plan_sha256") != state.get("plan_sha256"):
                    raise StorageInitializationError("state_intent_mismatch")
            self._verify_final_layout(
                plan,
                allow_device_growth=not intent_present,
                allow_runtime_docker_binds=not intent_present,
            )
        except (OSError, StorageInitializationError):
            return {"action": "status", "status": "drifted"}
        return {
            "action": "status",
            "change_id": plan["change_id"],
            "plan_sha256": state["plan_sha256"],
            "status": "initialized" if not intent_present else "finalization_required",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "plan/status are read-only. apply/resume require root, a controlling TTY, "
            "the exact live plan SHA-256, and a root-owned 0600 manifest. There is no "
            "force, wipe, rollback, generic device, or non-interactive option."
        ),
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_DEFAULT)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("plan")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--plan-sha256", required=True)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--plan-sha256", required=True)
    subparsers.add_parser("status")
    return parser


def _exit_code(result: Mapping[str, object]) -> int:
    return 0 if result.get("status") in {
        "ready",
        "initialized",
        "already_initialized",
    } else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    arguments = _parser().parse_args(argv)
    initializer = ProductionStorageInitializer(manifest_path=arguments.manifest)
    try:
        if arguments.action == "plan":
            result = initializer.plan()
        elif arguments.action == "apply":
            result = initializer.apply(arguments.plan_sha256)
        elif arguments.action == "resume":
            result = initializer.resume(arguments.plan_sha256)
        else:
            result = initializer.status()
    except StorageInitializationError as exc:
        print(
            json.dumps(
                {
                    "action": arguments.action,
                    "safe_code": exc.code,
                    "status": "blocked",
                },
                sort_keys=True,
            ),
            file=stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True), file=stdout)
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
