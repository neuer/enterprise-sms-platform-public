from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storage_preflight as storage_module  # noqa: E402
from storage_preflight import (  # noqa: E402
    DIRECTORY_REQUIREMENTS,
    FILESYSTEM_METADATA_TOLERANCE_PERCENT,
    GIB,
    MOUNT_REQUIREMENTS,
    PreflightMode,
    inspect_storage,
    parse_fstab,
)

PREFLIGHT = ROOT / "deploy" / "scripts" / "storage_preflight.py"
PREFLIGHT_UNIT = ROOT / "deploy" / "systemd" / "sms-storage-preflight.service"
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


@dataclass(frozen=True, slots=True)
class FakeStat:
    st_mode: int
    st_uid: int
    st_gid: int
    st_dev: int


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
    ) -> None:
        self.fstab = fstab
        self.mountinfo = mountinfo
        self.stats = stats
        self.filesystems = filesystems
        self.resolved: dict[Path, Path] = {}
        self.uuid_devices: dict[str, tuple[int, int]] = {}

    def read_text(self, path: Path) -> str:
        if path == Path("/etc/fstab"):
            return self.fstab
        if path == Path("/proc/self/mountinfo"):
            return self.mountinfo
        raise FileNotFoundError(path)

    def lstat(self, path: Path) -> FakeStat:
        try:
            return self.stats[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def statvfs(self, path: Path) -> FakeFilesystem:
        try:
            return self.filesystems[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def lexists(self, path: Path) -> bool:
        return path in self.stats

    def resolve(self, path: Path) -> Path:
        if path not in self.stats:
            raise FileNotFoundError(path)
        return self.resolved.get(path, path)

    def block_device_identity(self, uuid: str) -> tuple[int, int]:
        try:
            return self.uuid_devices[uuid]
        except KeyError as error:
            raise FileNotFoundError(uuid) from error


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
    for index, mount_requirement in enumerate(MOUNT_REQUIREMENTS, start=1):
        uuid = f"00000000-0000-0000-0000-{index:012d}"
        options = "defaults" if mount_requirement.path == Path("/") else "defaults,nodev,nosuid"
        active_options = (
            "rw,relatime"
            if mount_requirement.path == Path("/")
            else "rw,relatime,nodev,nosuid"
        )
        fstab_lines.append(
            f"UUID={uuid} {mount_requirement.path} xfs {options} 0 2"
        )
        mountinfo_lines.append(
            f"{index + 20} 20 8:{index} / {mount_requirement.path} {active_options} "
            f"- xfs /dev/disk{index} rw"
        )
        uuid_devices[uuid] = (8, index)
        mount_devices[mount_requirement.path] = index
        stats[mount_requirement.path] = fake_stat(
            mount_requirement.mode,
            mount_requirement.uid,
            mount_requirement.gid,
            index,
        )
        filesystems[mount_requirement.path] = fake_filesystem(
            mount_requirement.nominal_gib
        )
    for directory_requirement in DIRECTORY_REQUIREMENTS:
        stats[directory_requirement.path] = fake_stat(
            directory_requirement.mode,
            directory_requirement.uid,
            directory_requirement.gid,
            mount_devices[directory_requirement.mount_path],
        )
    probe = FakeProbe(
        fstab="\n".join(fstab_lines) + "\n",
        mountinfo="\n".join(mountinfo_lines) + "\n",
        stats=stats,
        filesystems=filesystems,
    )
    probe.uuid_devices = uuid_devices
    return probe


def finding_codes(probe: FakeProbe, *, mode: PreflightMode = "startup") -> set[str]:
    return {
        finding.code
        for finding in inspect_storage(probe, mode=mode).findings
    }


def test_fixed_storage_contract_matches_approved_vmdks_and_subpaths() -> None:
    assert [(str(item.path), item.nominal_gib) for item in MOUNT_REQUIREMENTS] == [
        ("/", 100),
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


def test_complete_distinct_storage_layout_passes() -> None:
    report = inspect_storage(valid_probe())

    assert report.passed is True
    assert report.findings == ()
    assert len(report.usages) == 5


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

    assert {"fstab_not_uuid", "fstab_weak_dependency"} <= codes


def test_fstab_rejects_manual_docker_internal_volume_relocation() -> None:
    probe = valid_probe()
    probe.fstab += (
        "/var/lib/sms-platform/postgres/pgdata "
        "/var/lib/docker/volumes/sms-platform_pgdata/_data none bind 0 0\n"
    )

    assert "docker_internal_volume_fstab" in finding_codes(probe)


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
    probe.uuid_devices[uuid] = (8, 99)

    assert "fstab_mount_identity_mismatch" in finding_codes(probe)


def test_unresolvable_fstab_uuid_fails_closed() -> None:
    probe = valid_probe()
    uuid = "00000000-0000-0000-0000-000000000004"
    del probe.uuid_devices[uuid]

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

    assert {"mounted_filesystem_not_allowed", "mount_read_only"} <= codes


def test_preflight_implementation_has_no_host_mutation_primitive() -> None:
    source = PREFLIGHT.read_text(encoding="utf-8")

    for forbidden in (
        "subprocess",
        "os.system",
        ".mkdir(",
        ".chmod(",
        ".chown(",
        "shutil",
    ):
        assert forbidden not in source


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
    assert "DevicePolicy=closed" in unit
    assert "ReadOnlyPaths=/etc/fstab /dev/disk/by-uuid" in unit
    assert "Requires=sms-storage-preflight.service" in docker_drop_in
    assert "After=sms-storage-preflight.service" in docker_drop_in
    assert f"RequiresMountsFor={mounts}" in platform_drop_in
    assert (
        "ExecStartPre=/usr/local/sbin/sms-storage-preflight --mode startup"
        in platform_drop_in
    )
    assert "ExecStartPre=-" not in platform_drop_in


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
