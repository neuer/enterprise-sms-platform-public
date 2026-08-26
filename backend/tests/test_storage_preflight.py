from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import initialize_production_storage as initializer_storage_module  # noqa: E402
import storage_preflight as storage_module  # noqa: E402
from storage_preflight import (  # noqa: E402
    DIRECTORY_REQUIREMENTS,
    DOCKER_BIND_MOUNT_REQUIREMENTS,
    FILESYSTEM_METADATA_TOLERANCE_PERCENT,
    GIB,
    MOUNT_REQUIREMENTS,
    PreflightMode,
    inspect_storage,
    parse_fstab,
)

PREFLIGHT = ROOT / "deploy" / "scripts" / "storage_preflight.py"
PREFLIGHT_UNIT = ROOT / "deploy" / "systemd" / "sms-storage-preflight.service"
BACKUP_UNIT = ROOT / "deploy" / "systemd" / "sms-backup.service"
RESTORE_DRILL_UNIT = ROOT / "deploy" / "systemd" / "sms-restore-drill.service"
LIFECYCLE_STATUS_UNIT = ROOT / "deploy" / "systemd" / "sms-lifecycle-status.service"
PARTITION_MAINTENANCE_UNIT = (
    ROOT / "deploy" / "systemd" / "sms-partition-maintenance.service"
)
DOCKER_DROP_IN = (
    ROOT / "deploy" / "systemd" / "docker.service.d" / "10-sms-platform-storage.conf"
)
PLATFORM_DROP_IN = (
    ROOT
    / "deploy"
    / "systemd"
    / "sms-platform.service.d"
    / "10-storage-preflight.conf"
)
RUNBOOK = ROOT / "deploy" / "storage.md"
WRAPPER = ROOT / "deploy" / "sms-compose"
PRODUCTION_STORAGE_COMPOSE = ROOT / "deploy" / "docker-compose.production-storage.yml"


@dataclass(frozen=True, slots=True)
class FakeStat:
    st_mode: int
    st_uid: int
    st_gid: int
    st_dev: int
    st_nlink: int = 1
    st_size: int = 0
    st_blocks: int = 0


@dataclass(frozen=True, slots=True)
class FakeFilesystem:
    f_frsize: int
    f_bsize: int
    f_blocks: int
    f_bfree: int
    f_bavail: int


class FakeProbe:
    def __init__(
        self,
        *,
        fstab: str,
        mountinfo: str,
        stats: dict[Path, FakeStat],
        filesystems: dict[Path, FakeFilesystem],
        proc_swaps: str = (
            "Filename Type Size Used Priority\n"
            "/swap.img file 8388604 0 -2\n"
        ),
    ) -> None:
        self.fstab = fstab
        self.mountinfo = mountinfo
        self.stats = stats
        self.filesystems = filesystems
        self.proc_swaps = proc_swaps
        self.resolved: dict[Path, Path] = {}
        self.uuid_devices: dict[str, tuple[int, int]] = {}
        self.xfs_ftypes: dict[Path, int] = {}
        self.fstab_verification_error: Exception | None = None
        self.source_devices: dict[str, tuple[int, int] | None] = {}
        self.host_context_error: Exception | None = None
        self.read_paths: list[Path] = []
        self.lstat_errors: dict[Path, OSError] = {}
        self.resolve_errors: dict[Path, OSError] = {}

    def assert_host_context(self) -> None:
        if self.host_context_error is not None:
            raise self.host_context_error

    def read_text(self, path: Path) -> str:
        self.read_paths.append(path)
        if path == Path("/etc/fstab"):
            return self.fstab
        if path == Path("/proc/swaps"):
            return self.proc_swaps
        raise FileNotFoundError(path)

    def read_host_mountinfo(self) -> str:
        self.read_paths.append(Path("/proc/1/mountinfo"))
        return self.mountinfo

    def lstat(self, path: Path) -> FakeStat:
        if path in self.lstat_errors:
            raise self.lstat_errors[path]
        try:
            return self.stats[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def statvfs(self, path: Path) -> FakeFilesystem:
        try:
            return self.filesystems[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def resolve(self, path: Path) -> Path:
        if path not in self.stats:
            raise FileNotFoundError(path)
        if path in self.resolve_errors:
            raise self.resolve_errors[path]
        return self.resolved.get(path, path)

    def block_device_identity(self, uuid: str) -> tuple[int, int]:
        try:
            return self.uuid_devices[uuid]
        except KeyError as error:
            raise FileNotFoundError(uuid) from error

    def xfs_ftype(self, path: Path) -> int:
        try:
            return self.xfs_ftypes[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def verify_fstab(self, path: Path) -> None:
        assert path == Path("/etc/fstab")
        if self.fstab_verification_error is not None:
            raise self.fstab_verification_error

    def fstab_source_identity(
        self,
        entry: storage_module.FstabEntry,
    ) -> tuple[int, int] | None:
        if entry.source == "/swap.img" and entry.fs_type == "swap":
            return None
        try:
            return self.source_devices[entry.source]
        except KeyError as error:
            raise FileNotFoundError(entry.source) from error


def fake_stat(mode: int, uid: int, gid: int, device_number: int) -> FakeStat:
    return FakeStat(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
        st_dev=os.makedev(8, device_number),
    )


def fake_filesystem(
    capacity_gib: int | float, used_percent: int = 10
) -> FakeFilesystem:
    block_size = 1024 * 1024
    blocks = int(capacity_gib * GIB // block_size)
    used = blocks * used_percent // 100
    free = blocks - used
    return FakeFilesystem(
        f_frsize=block_size,
        f_bsize=block_size,
        f_blocks=blocks,
        f_bfree=free,
        f_bavail=free,
    )


def valid_probe() -> FakeProbe:
    fstab_lines: list[str] = []
    mountinfo_lines: list[str] = []
    stats: dict[Path, FakeStat] = {}
    filesystems: dict[Path, FakeFilesystem] = {}
    mount_devices: dict[Path, int] = {}
    uuid_devices: dict[str, tuple[int, int]] = {}
    device_numbers = {
        Path("/"): 1,
        Path("/boot"): 6,
        Path("/boot/efi"): 7,
        Path("/var/lib/docker"): 2,
        Path("/var/lib/sms-platform/postgres"): 3,
        Path("/var/lib/sms-platform/redis"): 4,
        Path("/var/lib/sms-platform/runtime"): 5,
    }
    source_by_path = {
        Path("/"): "/dev/disk/by-id/dm-uuid-LVM-" + "A" * 64,
        Path("/boot"): "/dev/disk/by-uuid/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        Path("/boot/efi"): "/dev/disk/by-uuid/ABCD-1234",
    }
    for index, mount_requirement in enumerate(MOUNT_REQUIREMENTS, start=1):
        device_number = device_numbers[mount_requirement.path]
        uuid = f"00000000-0000-0000-0000-{device_number:012d}"
        source = source_by_path.get(mount_requirement.path, f"UUID={uuid}")
        options = (
            "defaults,nodev,nosuid"
            if mount_requirement.required_options
            else "defaults"
        )
        active_options = (
            "rw,relatime,nodev,nosuid"
            if mount_requirement.required_options
            else "rw,relatime"
        )
        fstab_lines.append(
            f"{source} {mount_requirement.path} {mount_requirement.fs_type} "
            f"{options} 0 {mount_requirement.pass_number}"
        )
        mountinfo_lines.append(
            f"{index + 20} 20 8:{device_number} / {mount_requirement.path} "
            f"{active_options} - {mount_requirement.fs_type} "
            f"/dev/disk{device_number} rw"
        )
        uuid_devices[uuid] = (8, device_number)
        mount_devices[mount_requirement.path] = device_number
        stats[mount_requirement.path] = fake_stat(
            mount_requirement.mode,
            mount_requirement.uid,
            mount_requirement.gid,
            device_number,
        )
        capacity_gib: int | float = mount_requirement.nominal_gib
        if mount_requirement.path == Path("/"):
            capacity_gib = 95
        elif mount_requirement.path == Path("/boot"):
            capacity_gib = 1.9
        elif mount_requirement.path == Path("/boot/efi"):
            capacity_gib = 1.04
        filesystems[mount_requirement.path] = fake_filesystem(
            capacity_gib
        )
    for directory_requirement in DIRECTORY_REQUIREMENTS:
        stats[directory_requirement.path] = fake_stat(
            directory_requirement.mode,
            directory_requirement.uid,
            directory_requirement.gid,
            mount_devices[directory_requirement.mount_path],
        )
    stats[storage_module.SWAP_FILE_PATH] = FakeStat(
        st_mode=stat.S_IFREG | 0o600,
        st_uid=0,
        st_gid=0,
        st_dev=os.makedev(8, device_numbers[Path("/")]),
        st_nlink=1,
        st_size=storage_module.SWAP_FILE_BYTES,
        st_blocks=storage_module.SWAP_FILE_BYTES // 512,
    )
    probe = FakeProbe(
        fstab="\n".join(fstab_lines)
        + "\n/swap.img none swap sw 0 0\n",
        mountinfo="\n".join(mountinfo_lines) + "\n",
        stats=stats,
        filesystems=filesystems,
    )
    probe.uuid_devices = uuid_devices
    for requirement in MOUNT_REQUIREMENTS:
        device_number = device_numbers[requirement.path]
        source = source_by_path.get(
            requirement.path,
            f"UUID=00000000-0000-0000-0000-{device_number:012d}",
        )
        probe.source_devices[source] = (8, device_number)
    probe.xfs_ftypes = {
        requirement.path: 1
        for requirement in MOUNT_REQUIREMENTS
        if requirement.fs_type == "xfs"
    }
    return probe


def finding_codes(probe: FakeProbe, *, mode: PreflightMode = "startup") -> set[str]:
    return {
        finding.code
        for finding in inspect_storage(probe, mode=mode).findings
    }


def test_fixed_storage_contract_matches_approved_vmdks_and_subpaths() -> None:
    assert [(str(item.path), item.nominal_gib) for item in MOUNT_REQUIREMENTS] == [
        ("/", 100),
        ("/boot", 2),
        ("/boot/efi", 1),
        ("/var/lib/docker", 250),
        ("/var/lib/sms-platform/postgres", 400),
        ("/var/lib/sms-platform/redis", 100),
        ("/var/lib/sms-platform/runtime", 200),
    ]
    assert {str(item.path) for item in DIRECTORY_REQUIREMENTS} == {
        "/var/lib/sms-platform/postgres/pgdata",
        "/var/lib/sms-platform/redis/broker",
        "/var/lib/sms-platform/redis/auth",
        "/var/lib/sms-platform/redis/control",
        "/var/lib/sms-platform/runtime/imports",
        "/var/lib/sms-platform/runtime/exports",
        "/var/lib/sms-platform/runtime/raw-spill",
        "/var/lib/sms-platform/runtime/backups",
    }
    backups = next(item for item in DIRECTORY_REQUIREMENTS if item.name == "backups")
    assert (backups.uid, backups.gid, backups.mode) == (0, 0, 0o700)
    assert FILESYSTEM_METADATA_TOLERANCE_PERCENT == 2
    assert [item.fs_type for item in MOUNT_REQUIREMENTS] == [
        "ext4",
        "ext4",
        "vfat",
        "xfs",
        "xfs",
        "xfs",
        "xfs",
    ]


def test_complete_distinct_storage_layout_passes() -> None:
    report = inspect_storage(valid_probe())

    assert report.passed is True
    assert report.findings == ()
    assert len(report.usages) == 7


def test_usage_telemetry_does_not_report_boot_partitions_as_vmdks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage_module._emit(inspect_storage(valid_probe()), "observe")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage = {
        record["mount"]: record
        for record in records
        if record["event"] == "storage_preflight_usage"
    }

    assert usage["boot"]["capacity_kind"] == "partition"
    assert usage["boot"]["nominal_vmdk_gib"] is None
    assert usage["efi"]["nominal_capacity_gib"] == 1
    assert usage["docker"]["capacity_kind"] == "vmdk"
    assert usage["docker"]["nominal_vmdk_gib"] == 250


def test_existing_ubuntu_lvm_root_and_boot_capacity_floors_are_explicit() -> None:
    probe = valid_probe()
    probe.filesystems[Path("/")] = fake_filesystem(94)
    probe.filesystems[Path("/boot")] = fake_filesystem(1.81)

    assert inspect_storage(probe).passed is True

    probe.filesystems[Path("/")] = fake_filesystem(94 - 1 / 1024)
    assert "filesystem_too_small" in finding_codes(probe)


def test_root_fstab_must_keep_the_frozen_dm_lvm_by_id_shape() -> None:
    probe = valid_probe()
    root_source = "/dev/disk/by-id/dm-uuid-LVM-" + "A" * 64
    probe.fstab = probe.fstab.replace(root_source, "UUID=" + "a" * 36)

    assert "fstab_source_contract_mismatch" in finding_codes(probe)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("missing-fstab", "swap_fstab_missing"),
        ("second-active-swap", "swap_state_contract_mismatch"),
        ("sparse-file", "swap_file_contract_mismatch"),
        ("wrong-mode", "swap_file_contract_mismatch"),
    ),
)
def test_swapfile_contract_fails_closed(
    mutation: str,
    expected_code: str,
) -> None:
    probe = valid_probe()
    if mutation == "missing-fstab":
        probe.fstab = probe.fstab.replace("/swap.img none swap sw 0 0\n", "")
    elif mutation == "second-active-swap":
        probe.proc_swaps += "/dev/sdz1 partition 1048572 0 -3\n"
    elif mutation == "sparse-file":
        probe.stats[storage_module.SWAP_FILE_PATH] = replace(
            probe.stats[storage_module.SWAP_FILE_PATH],
            st_blocks=1,
        )
    else:
        probe.stats[storage_module.SWAP_FILE_PATH] = replace(
            probe.stats[storage_module.SWAP_FILE_PATH],
            st_mode=stat.S_IFREG | 0o644,
        )

    assert expected_code in finding_codes(probe)


def test_findmnt_verify_accepts_clean_or_only_the_known_swap_warning() -> None:
    known_stdout = (
        b"none\n"
        b"   [W] non-bind mount source /swap.img is a directory or regular file\n"
    )
    known_stderr = b"\n0 parse errors, 0 errors, 1 warning\n"

    assert storage_module._findmnt_verify_output_is_accepted(b"", b"") is True
    assert storage_module._findmnt_verify_output_is_accepted(
        b"Success, no errors or warnings detected\n",
        b"",
    ) is True
    assert storage_module._findmnt_verify_output_is_accepted(
        known_stdout,
        known_stderr,
    ) is True
    assert storage_module._findmnt_verify_output_is_accepted(
        b"/mnt/alias\n [W] unexpected warning\n",
        known_stderr,
    ) is False


def test_unavailable_inspection_returns_failure_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*, mode: str) -> None:
        raise FileNotFoundError("/etc/fstab")

    monkeypatch.setattr(storage_module, "inspect_storage", unavailable)

    assert storage_module.main([]) == 1


def test_missing_mount_fails_closed() -> None:
    probe = valid_probe()
    probe.mountinfo = "\n".join(
        line
        for line in probe.mountinfo.splitlines()
        if " /var/lib/sms-platform/postgres " not in line
    )

    report = inspect_storage(probe)

    assert report.passed is False
    assert "mount_missing" in finding_codes(probe)


def test_fstab_requires_uuid_and_rejects_weak_mount_options() -> None:
    probe = valid_probe()
    probe.fstab = probe.fstab.replace(
        "UUID=00000000-0000-0000-0000-000000000003 "
        "/var/lib/sms-platform/postgres xfs defaults,nodev,nosuid",
        "/dev/sdc /var/lib/sms-platform/postgres xfs defaults,nodev,nosuid,nofail",
    )

    codes = finding_codes(probe)

    assert {"fstab_source_contract_mismatch", "fstab_weak_dependency"} <= codes


def test_fstab_rejects_read_only_boot_and_wrong_fsck_order() -> None:
    probe = valid_probe()
    root_source = "/dev/disk/by-id/dm-uuid-LVM-" + "A" * 64
    probe.fstab = probe.fstab.replace(
        f"{root_source} / ext4 defaults 0 1",
        f"{root_source} / ext4 defaults,ro 0 0",
    )

    codes = finding_codes(probe)

    assert {"fstab_weak_dependency", "fstab_check_order_mismatch"} <= codes


def test_preflight_reads_pid1_mountinfo_not_its_systemd_sandbox_view() -> None:
    probe = valid_probe()

    assert inspect_storage(probe).passed is True
    assert Path("/proc/1/mountinfo") in probe.read_paths
    assert Path("/proc/self/mountinfo") not in probe.read_paths


def test_untrusted_host_context_fails_closed_before_reading_storage() -> None:
    probe = valid_probe()
    probe.host_context_error = storage_module.StorageContractError(
        "not a production host"
    )

    with pytest.raises(storage_module.StorageContractError):
        inspect_storage(probe)

    assert probe.read_paths == []


def test_findmnt_warning_fails_daily_preflight_closed() -> None:
    probe = valid_probe()
    probe.fstab_verification_error = storage_module.StorageContractError(
        "findmnt warning"
    )

    assert "fstab_verification_failed" in finding_codes(probe)


def test_managed_sources_and_devices_cannot_have_extra_alias_mounts() -> None:
    probe = valid_probe()
    probe.fstab += (
        "UUID=00000000-0000-0000-0000-000000000002 "
        "/mnt/docker-alias xfs defaults,nodev,nosuid 0 2\n"
    )
    probe.mountinfo += (
        "99 20 8:2 / /mnt/docker-alias rw,relatime,nodev,nosuid "
        "- xfs /dev/disk2 rw\n"
    )

    codes = finding_codes(probe)

    assert {"fstab_managed_source_alias", "managed_device_mount_alias"} <= codes


def test_managed_source_alias_is_detected_before_mount_with_label_form() -> None:
    probe = valid_probe()
    probe.fstab += (
        "LABEL=sms_docker /mnt/docker-alias xfs defaults,nodev,nosuid 0 2\n"
    )
    probe.source_devices["LABEL=sms_docker"] = (8, 2)

    assert "fstab_managed_source_alias" in finding_codes(probe)


def test_tag_source_paths_preserve_case_for_vfat_uuid_and_labels() -> None:
    entries = parse_fstab(
        "UUID=ABCD-1234 /boot/efi vfat defaults 0 1\n"
        "LABEL=CaseSensitive /srv/data xfs defaults 0 2\n"
        "PARTLABEL=DataDisk /srv/part xfs defaults 0 2\n"
    )

    assert [storage_module._fstab_source_path(entry) for entry in entries] == [
        Path("/dev/disk/by-uuid/ABCD-1234"),
        Path("/dev/disk/by-label/CaseSensitive"),
        Path("/dev/disk/by-partlabel/DataDisk"),
    ]


def test_standard_uppercase_vfat_uuid_does_not_block_reverse_scan() -> None:
    probe = valid_probe()

    assert inspect_storage(probe).passed is True


def test_exact_docker_local_driver_bind_mounts_are_approved() -> None:
    probe = valid_probe()
    devices = {
        Path("/var/lib/sms-platform/postgres"): 3,
        Path("/var/lib/sms-platform/redis"): 4,
        Path("/var/lib/sms-platform/runtime"): 5,
    }
    for index, requirement in enumerate(
        DOCKER_BIND_MOUNT_REQUIREMENTS,
        start=100,
    ):
        device = devices[requirement.source_mount_path]
        probe.stats[requirement.target_path] = fake_stat(0o700, 0, 0, device)
        probe.mountinfo += (
            f"{index} 20 8:{device} {requirement.filesystem_root} "
            f"{requirement.target_path} rw,relatime,nodev,nosuid "
            f"- xfs /dev/disk{device} rw\n"
        )

    assert inspect_storage(probe).passed is True


def test_docker_bind_allowlist_matches_production_compose_contract() -> None:
    compose = PRODUCTION_STORAGE_COMPOSE.read_text(encoding="utf-8")

    for requirement in DOCKER_BIND_MOUNT_REQUIREMENTS:
        assert requirement.target_path.parent.name == f"sms-platform_{requirement.name}"
        assert re.search(
            rf"(?m)^  {re.escape(requirement.name)}:\n"
            rf"(?:    .*\n)*?      device: {re.escape(str(requirement.source_path))}$",
            compose,
        )

    preflight_contract = {
        (
            requirement.source_path,
            requirement.target_path,
            requirement.filesystem_root,
        )
        for requirement in DOCKER_BIND_MOUNT_REQUIREMENTS
    }
    initializer_contract = {
        (requirement.source_path, requirement.target_path, requirement.filesystem_root)
        for requirement in initializer_storage_module.DOCKER_BIND_MOUNT_SPECS
    }
    assert preflight_contract == initializer_contract


@pytest.mark.parametrize(
    ("filesystem_root", "options"),
    (
        ("/wrong-root", "rw,relatime,nodev,nosuid"),
        ("/pgdata", "rw,relatime,nodev"),
    ),
)
def test_docker_bind_contract_mismatch_is_blocking(
    filesystem_root: str,
    options: str,
) -> None:
    probe = valid_probe()
    requirement = DOCKER_BIND_MOUNT_REQUIREMENTS[0]
    probe.stats[requirement.target_path] = fake_stat(0o700, 0, 0, 3)
    probe.mountinfo += (
        f"100 20 8:3 {filesystem_root} {requirement.target_path} {options} "
        "- xfs /dev/disk3 rw\n"
    )

    assert "docker_bind_mount_contract_mismatch" in finding_codes(probe)


def test_duplicate_approved_docker_bind_target_is_blocking() -> None:
    probe = valid_probe()
    requirement = DOCKER_BIND_MOUNT_REQUIREMENTS[0]
    probe.stats[requirement.target_path] = fake_stat(0o700, 0, 0, 3)
    line = (
        f"100 20 8:3 {requirement.filesystem_root} {requirement.target_path} "
        "rw,relatime,nodev,nosuid - xfs /dev/disk3 rw\n"
    )
    probe.mountinfo += line + line.replace("100 20", "101 20")

    assert "docker_bind_mount_contract_mismatch" in finding_codes(probe)


def test_approved_docker_target_on_wrong_device_is_blocking() -> None:
    probe = valid_probe()
    requirement = DOCKER_BIND_MOUNT_REQUIREMENTS[0]
    probe.stats[requirement.target_path] = fake_stat(0o700, 0, 0, 99)
    probe.mountinfo += (
        f"100 20 8:99 {requirement.filesystem_root} {requirement.target_path} "
        "rw,relatime,nodev,nosuid - xfs /dev/disk99 rw\n"
    )

    assert "docker_bind_mount_contract_mismatch" in finding_codes(probe)


def test_fixed_mount_must_expose_filesystem_root() -> None:
    probe = valid_probe()
    probe.mountinfo = probe.mountinfo.replace(
        "8:2 / /var/lib/docker",
        "8:2 /subdirectory /var/lib/docker",
    )

    assert "mount_filesystem_root_mismatch" in finding_codes(probe)


def test_fstab_rejects_manual_docker_internal_volume_relocation() -> None:
    probe = valid_probe()
    probe.fstab += (
        "/var/lib/sms-platform/postgres/pgdata "
        "/var/lib/docker/volumes/sms-platform_pgdata/_data none bind 0 0\n"
    )

    assert "docker_internal_volume_fstab" in finding_codes(probe)


@pytest.mark.parametrize(
    ("mode", "uid"),
    (
        (stat.S_IFLNK | 0o777, 0),
        (stat.S_IFDIR | 0o777, 0),
        (stat.S_IFDIR | 0o600, 0),
        (stat.S_IFDIR | 0o755, 1000),
    ),
)
def test_docker_volume_control_ancestors_must_be_canonical_and_root_controlled(
    mode: int,
    uid: int,
) -> None:
    probe = valid_probe()
    path = Path("/var/lib/docker/volumes")
    probe.stats[path] = FakeStat(
        st_mode=mode,
        st_uid=uid,
        st_gid=0,
        st_dev=os.makedev(8, 2),
    )
    if stat.S_ISLNK(mode):
        probe.resolved[path] = Path("/mnt/untrusted-volumes")

    assert "docker_volume_control_path_unsafe" in finding_codes(probe)


def test_inaccessible_docker_volume_control_path_fails_closed() -> None:
    probe = valid_probe()
    path = Path("/var/lib/docker/volumes")
    probe.lstat_errors[path] = PermissionError(path)

    assert "docker_volume_control_path_unsafe" in finding_codes(probe)


def test_dangling_docker_volume_control_symlink_fails_closed() -> None:
    probe = valid_probe()
    path = Path("/var/lib/docker/volumes")
    probe.stats[path] = FakeStat(
        st_mode=stat.S_IFLNK | 0o777,
        st_uid=0,
        st_gid=0,
        st_dev=os.makedev(8, 2),
    )
    probe.resolve_errors[path] = FileNotFoundError(path)

    assert "docker_volume_control_path_unsafe" in finding_codes(probe)


def test_missing_or_secure_docker_volume_control_ancestors_are_allowed() -> None:
    probe = valid_probe()
    root = Path("/var/lib/docker/volumes")
    volume = Path("/var/lib/docker/volumes/sms-platform_pgdata")
    probe.stats[root] = fake_stat(0o711, 0, 0, 2)
    probe.stats[volume] = fake_stat(0o755, 0, 0, 2)

    assert inspect_storage(probe).passed is True


def test_docker_volume_control_directories_must_stay_on_docker_vmdk() -> None:
    probe = valid_probe()
    path = Path("/var/lib/docker/volumes")
    probe.stats[path] = fake_stat(0o711, 0, 0, 99)

    assert "docker_volume_control_device_mismatch" in finding_codes(probe)


def test_docker_volume_control_directories_must_not_be_nested_mounts() -> None:
    probe = valid_probe()
    path = Path("/var/lib/docker/volumes")
    probe.stats[path] = fake_stat(0o711, 0, 0, 2)
    probe.mountinfo += (
        f"100 20 8:2 /volumes {path} rw,relatime,nodev,nosuid "
        "- xfs /dev/disk2 rw\n"
    )

    codes = finding_codes(probe)

    assert "docker_volume_control_nested_mount" in codes


def test_storage_devices_must_remain_distinct() -> None:
    probe = valid_probe()
    redis_path = Path("/var/lib/sms-platform/redis")
    postgres_path = Path("/var/lib/sms-platform/postgres")
    probe.mountinfo = probe.mountinfo.replace(
        "8:4 / /var/lib/sms-platform/redis",
        "8:3 / /var/lib/sms-platform/redis",
    )
    probe.stats[redis_path] = replace(
        probe.stats[redis_path], st_dev=probe.stats[postgres_path].st_dev
    )

    assert "shared_failure_domain" in finding_codes(probe)


def test_fstab_uuid_must_resolve_to_current_mount_device() -> None:
    probe = valid_probe()
    uuid = "00000000-0000-0000-0000-000000000004"
    probe.source_devices[f"UUID={uuid}"] = (8, 99)

    assert "fstab_mount_identity_mismatch" in finding_codes(probe)


def test_unresolvable_fstab_uuid_fails_closed() -> None:
    probe = valid_probe()
    uuid = "00000000-0000-0000-0000-000000000004"
    del probe.source_devices[f"UUID={uuid}"]

    assert "fstab_uuid_unresolved" in finding_codes(probe)


def test_data_mounts_require_nodev_and_nosuid_in_fstab_and_mountinfo() -> None:
    probe = valid_probe()
    probe.fstab = probe.fstab.replace(
        "/var/lib/sms-platform/redis xfs defaults,nodev,nosuid",
        "/var/lib/sms-platform/redis xfs defaults,nodev",
    )
    probe.mountinfo = probe.mountinfo.replace(
        "/var/lib/sms-platform/runtime rw,relatime,nodev,nosuid",
        "/var/lib/sms-platform/runtime rw,relatime,nosuid",
    )

    codes = finding_codes(probe)

    assert {"fstab_safety_options_missing", "mount_safety_options_missing"} <= codes


def test_nominal_vmdk_allows_small_filesystem_metadata_tolerance() -> None:
    probe = valid_probe()
    probe.filesystems[Path("/var/lib/sms-platform/redis")] = fake_filesystem(99)

    assert inspect_storage(probe).passed is True


def test_filesystem_clearly_below_nominal_vmdk_tolerance_fails_closed() -> None:
    probe = valid_probe()
    probe.filesystems[Path("/var/lib/sms-platform/redis")] = fake_filesystem(97)

    report = inspect_storage(probe)

    assert report.passed is False
    assert "filesystem_too_small" in finding_codes(probe)


@pytest.mark.parametrize(
    ("mode", "used_percent", "expected_code", "expected_passed"),
    (
        ("observe", 70, "usage_warning", True),
        ("observe", 80, "usage_critical", True),
        ("observe", 90, "usage_emergency", True),
        ("startup", 80, "usage_critical", True),
        ("startup", 90, "usage_emergency", False),
        ("release", 80, "usage_critical", False),
        ("release", 90, "usage_emergency", False),
    ),
)
def test_usage_thresholds_are_exact(
    mode: PreflightMode,
    used_percent: int,
    expected_code: str,
    expected_passed: bool,
) -> None:
    probe = valid_probe()
    probe.filesystems[Path("/var/lib/docker")] = fake_filesystem(250, used_percent)

    report = inspect_storage(probe, mode=mode)

    assert report.passed is expected_passed
    assert expected_code in {finding.code for finding in report.findings}


def test_fixed_subpath_owner_mode_and_device_are_enforced() -> None:
    probe = valid_probe()
    path = Path("/var/lib/sms-platform/redis/auth")
    probe.stats[path] = fake_stat(0o755, 0, 0, 9)

    codes = finding_codes(probe)

    assert {"wrong_owner", "wrong_mode", "subpath_wrong_filesystem"} <= codes


def test_symlinked_required_subpath_fails_closed() -> None:
    probe = valid_probe()
    path = Path("/var/lib/sms-platform/runtime/exports")
    probe.stats[path] = replace(probe.stats[path], st_mode=stat.S_IFLNK | 0o777)

    assert "symlink_forbidden" in finding_codes(probe)


def test_symlinked_ancestor_fails_closed() -> None:
    probe = valid_probe()
    path = Path("/var/lib/sms-platform/runtime/imports")
    probe.resolved[path] = Path("/mnt/unapproved/imports")

    assert "path_not_canonical" in finding_codes(probe)


def test_unapproved_or_read_only_filesystem_fails_closed() -> None:
    probe = valid_probe()
    probe.mountinfo = probe.mountinfo.replace(
        "8:4 / /var/lib/sms-platform/redis rw,relatime,nodev,nosuid - xfs",
        "8:4 / /var/lib/sms-platform/redis ro,relatime,nodev,nosuid - nfs",
    )

    codes = finding_codes(probe)

    assert {"mounted_filesystem_mismatch", "mount_read_only"} <= codes


def test_filesystem_types_are_role_specific_in_fstab_and_mountinfo() -> None:
    probe = valid_probe()
    target = "/var/lib/sms-platform/postgres"
    probe.fstab = probe.fstab.replace(f"{target} xfs", f"{target} ext4")
    probe.mountinfo = probe.mountinfo.replace(
        f"{target} rw,relatime,nodev,nosuid - xfs",
        f"{target} rw,relatime,nodev,nosuid - ext4",
    )

    codes = finding_codes(probe)

    assert {"fstab_filesystem_mismatch", "mounted_filesystem_mismatch"} <= codes


def test_every_data_xfs_requires_ftype_one() -> None:
    probe = valid_probe()
    redis_path = Path("/var/lib/sms-platform/redis")
    probe.xfs_ftypes[redis_path] = 0

    assert "xfs_ftype_required" in finding_codes(probe)


def test_preflight_implementation_has_no_host_mutation_primitive() -> None:
    source = PREFLIGHT.read_text(encoding="utf-8")

    for forbidden in (
        "os.system",
        ".mkdir(",
        ".chmod(",
        ".chown(",
        "shutil",
    ):
        assert forbidden not in source
    assert "shell=False" in source
    assert storage_module.XFS_INFO_BINARY.startswith("/usr/sbin/")
    assert Path("/proc/1/mountinfo") == storage_module.MOUNTINFO_PATH
    assert storage_module.FINDMNT_BINARY == "/usr/bin/findmnt"


def test_systemd_assets_gate_docker_and_every_platform_start() -> None:
    unit = PREFLIGHT_UNIT.read_text(encoding="utf-8")
    docker_drop_in = DOCKER_DROP_IN.read_text(encoding="utf-8")
    platform_drop_in = PLATFORM_DROP_IN.read_text(encoding="utf-8")
    mounts = (
        "/var/lib/docker /var/lib/sms-platform/postgres "
        "/var/lib/sms-platform/redis /var/lib/sms-platform/runtime"
    )

    assert f"RequiresMountsFor={mounts}" in unit
    assert "Before=docker.service sms-platform.service" in unit
    assert "ExecStart=/usr/local/sbin/sms-storage-preflight --mode startup" in unit
    assert "PrivateDevices=yes" not in unit
    assert "ProtectSystem=strict" in unit
    assert "DevicePolicy=closed" in unit
    assert "DeviceAllow=block-sd r" in unit
    assert "DeviceAllow=block-device-mapper r" in unit
    assert "DeviceAllow=block-device-mapper rw" not in unit
    assert "TimeoutStartSec=45" in unit
    assert "ReadOnlyPaths=/etc/fstab /dev/disk/by-uuid /dev/disk/by-id" in unit
    assert "Requires=sms-storage-preflight.service" in docker_drop_in
    assert "After=sms-storage-preflight.service" in docker_drop_in
    assert f"RequiresMountsFor={mounts}" in platform_drop_in
    assert (
        "ExecStartPre=/usr/local/sbin/sms-storage-preflight --mode startup"
        in platform_drop_in
    )
    assert "ExecStartPre=-" not in platform_drop_in


def test_every_hardened_preflight_caller_has_read_only_pvscsi_access() -> None:
    for path in (
        BACKUP_UNIT,
        RESTORE_DRILL_UNIT,
        LIFECYCLE_STATUS_UNIT,
        PARTITION_MAINTENANCE_UNIT,
    ):
        unit = path.read_text(encoding="utf-8")
        assert "DevicePolicy=closed" in unit
        assert "DeviceAllow=block-sd r" in unit
        assert "DeviceAllow=block-device-mapper r" in unit
        assert "DeviceAllow=block-sd rw" not in unit
        assert "DeviceAllow=block-device-mapper rw" not in unit
        assert "DeviceAllow=block-*" not in unit
        assert "/dev/disk/by-id" in unit
    partition = PARTITION_MAINTENANCE_UNIT.read_text(encoding="utf-8")
    assert "PrivateDevices=yes" not in partition
    assert (
        "ReadOnlyPaths=/opt/sms-platform /etc/sms-platform /dev/disk/by-uuid "
        "/dev/disk/by-id"
    ) in partition


def test_runbook_covers_provisioning_thresholds_expansion_and_no_go() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for token in (
        "UUID=",
        "nofail",
        ">=70%",
        ">=80%",
        ">=90%",
        "xfs_growfs",
        "resize2fs",
        "/var/lib/docker/volumes",
        "禁止用 symlink",
        "不得以 VMware snapshot 代替数据库",
    ):
        assert token in runbook
    assert "Compose override" in runbook
    assert "driver" in runbook


def test_wrapper_uses_startup_policy_and_release_mutations_use_release_policy() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")

    assert 'python3 "$STORAGE_PREFLIGHT" --mode "$storage_mode"' in wrapper
    assert 'validate_production_launch release' in wrapper


def test_fstab_parser_rejects_malformed_non_comment_lines() -> None:
    with pytest.raises(ValueError, match="invalid fstab"):
        parse_fstab("UUID=broken /data\n")
