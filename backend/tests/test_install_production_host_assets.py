from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def installer_module() -> ModuleType:
    return importlib.import_module("install_production_host_assets")


def fixture_checkout_uid() -> int:
    """Return a real non-root owner when the test process itself runs as root."""

    return 1000 if os.geteuid() == 0 else os.geteuid()


COMMIT = "555fb20b0d630ece9099a88a463eb1ce1121c012"
POST_FIRST_START_COMMIT = "109c10865b2aac3989bc4cebf3c60788f44b168c"
NEW_COMMIT = POST_FIRST_START_COMMIT
POST_FIRST_START_TARGET_COMMIT = "c" * 40
OLD_DOCKER_MODE_CONTRACT = b'"docker", DOCKER_MOUNT_PATH, 250, 0, 0, 0o711, "xfs", "uuid-tag", 2,'
NEW_DOCKER_MODE_CONTRACT = b'"docker", DOCKER_MOUNT_PATH, 250, 0, 0, 0o710, "xfs", "uuid-tag", 2,'


@dataclass(frozen=True)
class TrackedAsset:
    mode: str
    payload: bytes


class FakeRunner:
    def __init__(
        self,
        module: ModuleType,
        source_root: Path,
        tracked: dict[str, TrackedAsset],
    ) -> None:
        self.module = module
        self.source_root = source_root
        self.tracked = tracked
        self.head_commit = COMMIT
        self.calls: list[tuple[str, ...]] = []
        self.dirty = False
        self.storage_passed = True
        self.active_service: str | None = None
        self.docker_load_states = {
            "docker.service": "masked",
            "docker.socket": "masked",
            "containerd.service": "masked",
        }
        self.platform_load_state = "not-found"
        self.platform_absent_returncode = 4
        self.platform_absent_active_state = "unknown"
        self.docker_filesystem = "ext4"
        self.git_sudo_uids: list[str | None] = []
        self.enabled_states: dict[str, tuple[int, str]] = {
            "sms-platform.service": (1, "disabled"),
            "vendor-control-agent.service": (1, "disabled"),
            "sms-partition-maintenance.timer": (1, "disabled"),
            "sms-backup.timer": (1, "disabled"),
            "sms-restore-drill.timer": (0, "static"),
            "sms-lifecycle-status.timer": (1, "disabled"),
        }
        self.storage_invocation_id = "1" * 32
        self.storage_result = "success"
        self.storage_exec_main_status = "0"
        self.storage_start_returncode = 0
        self.storage_start_attempted = False
        self.storage_state_before = ("inactive", "dead", "")
        self.storage_state_after = ("inactive", "dead", "")
        self.loaded_capabilities: dict[str, str] = {}
        self.loaded_environments: dict[str, str] = {}
        self.loaded_fragment_paths: dict[str, str] = {}
        self.need_daemon_reload: dict[str, str] = {}

    def run(  # type: ignore[no-untyped-def]
        self,
        argv: tuple[str, ...],
        *,
        git_sudo_uid: str | None = None,
        timeout_seconds: int = 30,
    ):
        self.calls.append(argv)
        if argv[:3] == (self.module.GIT_BINARY, "-C", str(self.source_root)):
            self.git_sudo_uids.append(git_sudo_uid)
            assert argv[3:7] == (
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
            )
            arguments = argv[7:]
            if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
                return self.module.CommandResult(0, f"{self.head_commit}\n".encode())
            if arguments == ("cat-file", "-t", self.head_commit):
                return self.module.CommandResult(0, b"commit\n")
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return self.module.CommandResult(0, b"?? unexpected\n" if self.dirty else b"")
            if arguments[:3] == ("ls-tree", "-z", self.head_commit):
                relative = arguments[-1]
                item = self.tracked[relative]
                return self.module.CommandResult(
                    0,
                    f"{item.mode} blob {'b' * 40}\t{relative}\0".encode(),
                )
            if arguments[:2] == ("cat-file", "blob"):
                relative = arguments[-1].split(":", 1)[1]
                return self.module.CommandResult(0, self.tracked[relative].payload)
        assert git_sudo_uid is None
        if argv[:3] == (self.module.PYTHON_BINARY, "-I", "-c"):
            assert argv[3].encode() == self.tracked["deploy/scripts/storage_preflight.py"].payload
            assert argv[4:] == ("--mode", "startup")
            return self.module.CommandResult(0 if self.storage_passed else 1)
        if argv[:2] == (self.module.SYSTEMCTL_BINARY, "show"):
            property_name = argv[2].removeprefix("--property=")
            unit = argv[4]
            if property_name == "LoadState":
                state = self.docker_load_states.get(unit, self.platform_load_state)
                if unit in self.loaded_capabilities:
                    state = "loaded"
                return self.module.CommandResult(0, f"{state}\n".encode())
            if unit == "sms-storage-preflight.service":
                values = {
                    "InvocationID": self.storage_invocation_id,
                    "Result": self.storage_result,
                    "ExecMainStatus": self.storage_exec_main_status,
                }
                active_state, sub_state, job = (
                    self.storage_state_after
                    if self.storage_start_attempted
                    else self.storage_state_before
                )
                values.update(
                    {
                        "ActiveState": active_state,
                        "SubState": sub_state,
                        "Job": job,
                    }
                )
                if property_name in values:
                    return self.module.CommandResult(0, f"{values[property_name]}\n".encode())
            if property_name == "CapabilityBoundingSet":
                return self.module.CommandResult(
                    0, f"{self.loaded_capabilities.get(unit, '')}\n".encode()
                )
            if property_name == "FragmentPath" and unit in self.loaded_capabilities:
                return self.module.CommandResult(
                    0, f"{self.loaded_fragment_paths.get(unit, '')}\n".encode()
                )
            if property_name == "NeedDaemonReload" and unit in self.loaded_capabilities:
                return self.module.CommandResult(
                    0, f"{self.need_daemon_reload.get(unit, 'no')}\n".encode()
                )
            if property_name == "Environment" and unit in self.loaded_capabilities:
                return self.module.CommandResult(
                    0,
                    (
                        self.loaded_environments.get(
                            unit, "SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL=1"
                        )
                        + "\n"
                    ).encode(),
                )
            return self.module.CommandResult(0, b"\n")
        if argv == (
            self.module.SYSTEMCTL_BINARY,
            "--system",
            "--no-ask-password",
            "--job-mode=fail",
            "start",
            "sms-storage-preflight.service",
        ):
            assert timeout_seconds == self.module.UPGRADE_ACCEPTANCE_TIMEOUT_SECONDS
            self.storage_start_attempted = True
            return self.module.CommandResult(self.storage_start_returncode)
        assert timeout_seconds == self.module.COMMAND_TIMEOUT_SECONDS
        if argv[:2] == (self.module.SYSTEMCTL_BINARY, "is-active"):
            service = argv[2]
            if service == self.active_service:
                return self.module.CommandResult(0, b"active\n")
            if service == "sms-platform.service" and self.platform_load_state == "not-found":
                return self.module.CommandResult(
                    self.platform_absent_returncode,
                    f"{self.platform_absent_active_state}\n".encode(),
                )
            return self.module.CommandResult(3, b"inactive\n")
        if argv[:2] == (self.module.SYSTEMCTL_BINARY, "is-enabled"):
            returncode, state = self.enabled_states.get(argv[2], (1, "disabled"))
            return self.module.CommandResult(returncode, f"{state}\n".encode())
        if argv[:5] == (
            self.module.FINDMNT_BINARY,
            "--noheadings",
            "--output",
            "FSTYPE",
            "--target",
        ):
            return self.module.CommandResult(0, f"{self.docker_filesystem}\n".encode())
        raise AssertionError(f"unexpected command: {argv!r}")


@dataclass
class Fixture:
    module: ModuleType
    installer: object
    runner: FakeRunner
    source_root: Path
    etc_root: Path
    systemd_root: Path
    local_sbin_root: Path
    docker_root: Path
    releases_root: Path
    state_path: Path


def make_fixture(tmp_path: Path, *, effective_uid: int = 0) -> Fixture:
    module = installer_module()
    checkout_uid = fixture_checkout_uid()
    source_root = tmp_path / "source"
    etc_root = tmp_path / "host/etc/sms-platform"
    systemd_root = tmp_path / "host/etc/systemd/system"
    local_sbin_root = tmp_path / "host/usr/local/sbin"
    docker_root = tmp_path / "host/var/lib/docker"
    releases_root = tmp_path / "host/var/lib/sms-platform/releases"
    systemd_root.mkdir(parents=True, mode=0o755)
    local_sbin_root.mkdir(parents=True, mode=0o755)
    docker_root.mkdir(parents=True, mode=0o710)
    docker_root.chmod(0o710)
    specs = module.build_asset_specs(
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
    )
    tracked: dict[str, TrackedAsset] = {}
    for spec in specs:
        source = source_root / spec.source_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        if spec.name in {
            "storage-unit",
            "backup-service",
            "restore-drill-service",
            "lifecycle-status-service",
        }:
            payload = (
                f"[Unit]\nDescription=fixed {spec.name}\n[Service]\nCapabilityBoundingSet=\n"
            ).encode()
        elif spec.name == "partition-service":
            payload = (
                f"[Unit]\nDescription=fixed {spec.name}\n"
                "[Service]\n"
                "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER\n"
            ).encode()
        else:
            payload = f"fixed {spec.name}\n".encode()
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode,
            payload=payload,
        )
    if checkout_uid != os.geteuid():
        os.chown(source_root, checkout_uid, -1, follow_symlinks=False)
    runner = FakeRunner(module, source_root, tracked)
    state_path = etc_root / "production-host-assets.json"
    installer = module.ProductionHostAssetInstaller(
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
        docker_root=docker_root,
        releases_root=releases_root,
        state_path=state_path,
        runner=runner,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
        effective_uid=effective_uid,
        invocation_environment={"SUDO_UID": str(checkout_uid)},
    )
    return Fixture(
        module=module,
        installer=installer,
        runner=runner,
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
        docker_root=docker_root,
        releases_root=releases_root,
        state_path=state_path,
    )


def apply(fixture: Fixture) -> dict[str, object]:
    return fixture.installer.apply(  # type: ignore[no-any-return,union-attr]
        COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )


UPGRADE_NAMES = (
    "storage-preflight",
    "storage-unit",
    "partition-service",
    "backup-service",
    "restore-drill-service",
    "lifecycle-status-service",
)


def seed_legacy_v1_install(fixture: Fixture, *, source_commit: str = COMMIT) -> None:
    """Write the exact schema-v1 shape deployed by the pre-upgrade installer."""

    if source_commit == COMMIT:
        fixture.docker_root.chmod(0o711)
    fixture.etc_root.mkdir(parents=True, mode=0o700)
    fixture.etc_root.chmod(0o700)
    (fixture.systemd_root / "docker.service.d").mkdir(mode=0o755)
    (fixture.systemd_root / "sms-platform.service.d").mkdir(mode=0o755)
    assets: list[dict[str, object]] = []
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        payload = (fixture.source_root / spec.source_relative).read_bytes()
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        if spec.kind == "regular":
            spec.destination.write_bytes(payload)
            spec.destination.chmod(spec.mode)
        else:
            spec.destination.symlink_to(spec.symlink_target)
        item: dict[str, object] = {
            "destination": str(spec.destination),
            "kind": spec.kind,
            "mode": f"{spec.mode:04o}",
            "name": spec.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if spec.symlink_target is not None:
            item["target"] = str(spec.symlink_target)
        assets.append(item)
    payload = (
        json.dumps(
            {
                "assets": assets,
                "schema_version": 1,
                "source_commit": source_commit,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    fixture.state_path.write_text(payload, encoding="ascii")
    fixture.state_path.chmod(0o600)
    fixture.runner.platform_load_state = "loaded"


def checkout_upgrade_target(
    fixture: Fixture, *, changed_names: tuple[str, ...] = UPGRADE_NAMES
) -> None:
    by_name = {
        spec.name: spec
        for spec in fixture.installer.assets  # type: ignore[union-attr]
    }
    for name in changed_names:
        spec = by_name[name]
        source = fixture.source_root / spec.source_relative
        if name == "storage-preflight":
            payload = (
                b"SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL = '1'\n"
                b"CREDENTIALS_DIRECTORY = '/run/credentials/unit'\n"
                b"MOUNTINFO_CREDENTIAL = 'sms-host-mountinfo'\n"
            )
        else:
            old_payload = source.read_bytes()
            capability = next(
                line
                for line in old_payload.splitlines()
                if line.startswith(b"CapabilityBoundingSet=")
            )
            payload = b"\n".join(
                (
                    f"[Unit]\nDescription=upgraded {name}".encode(),
                    b"[Service]",
                    capability,
                    b"LoadCredential=sms-host-mountinfo:/proc/1/mountinfo",
                    b"Environment=SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL=1",
                    b"",
                )
            )
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode,
            payload=payload,
        )
    fixture.runner.head_commit = NEW_COMMIT


def prepare_upgrade(fixture: Fixture) -> None:
    seed_legacy_v1_install(fixture)
    checkout_upgrade_target(fixture)


def prepare_post_first_start_repair(fixture: Fixture) -> bytes:
    """Seed 109c108 state, retain Docker metadata, and change only mode 0711→0710."""

    spec = next(
        item
        for item in fixture.installer.assets  # type: ignore[union-attr]
        if item.name == "storage-preflight"
    )
    source = fixture.source_root / spec.source_relative
    old_payload = b"MOUNT_REQUIREMENTS = (\n    " + OLD_DOCKER_MODE_CONTRACT + b"\n)\n"
    source.write_bytes(old_payload)
    source.chmod(int(spec.git_mode[-3:], 8))
    fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
        mode=spec.git_mode,
        payload=old_payload,
    )
    seed_legacy_v1_install(fixture, source_commit=POST_FIRST_START_COMMIT)

    new_payload = old_payload.replace(OLD_DOCKER_MODE_CONTRACT, NEW_DOCKER_MODE_CONTRACT, 1)
    source.write_bytes(new_payload)
    source.chmod(int(spec.git_mode[-3:], 8))
    fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
        mode=spec.git_mode,
        payload=new_payload,
    )
    fixture.runner.head_commit = POST_FIRST_START_TARGET_COMMIT
    (fixture.docker_root / "image").mkdir()
    (fixture.docker_root / "engine-id").write_text("retained\n", encoding="utf-8")
    return old_payload


def set_upgrade_acceptance_evidence(fixture: Fixture) -> None:
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        if spec.name not in UPGRADE_NAMES or spec.destination.suffix != ".service":
            continue
        capability_line = next(
            line
            for line in spec.destination.read_text(encoding="utf-8").splitlines()
            if line.startswith("CapabilityBoundingSet=")
        )
        fixture.runner.loaded_capabilities[spec.destination.name] = " ".join(
            token.lower() for token in capability_line.partition("=")[2].split()
        )
        fixture.runner.loaded_fragment_paths[spec.destination.name] = str(spec.destination)
    # systemd 255 clears InvocationID after a successful Type=oneshot service
    # without RemainAfterExit. Acceptance must not depend on that transient field.
    fixture.runner.storage_invocation_id = ""


def test_manifest_is_exactly_seventeen_regular_assets_and_one_wrapper_symlink(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    specs = fixture.installer.assets  # type: ignore[union-attr]

    assert len(specs) == 18
    assert sum(spec.kind == "regular" for spec in specs) == 17
    symlinks = [spec for spec in specs if spec.kind == "symlink"]
    assert len(symlinks) == 1
    assert symlinks[0].destination == fixture.local_sbin_root / "sms-compose"
    assert symlinks[0].symlink_target == fixture.source_root / "deploy/sms-compose"
    assert {spec.destination.name for spec in specs if spec.kind == "regular"} >= {
        "compose.env",
        "sms-storage-preflight",
        "sms-platform.service",
        "vendor-control-agent.service",
        "lifecycle.json",
        "lifecycle.env",
    }


def test_plan_is_read_only_and_checks_commit_storage_and_inactive_services(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert fixture.installer.plan(COMMIT) == {  # type: ignore[union-attr]
        "action": "plan",
        "assets": 18,
        "source_commit": COMMIT,
        "status": "ready",
    }

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before
    assert (
        fixture.module.PYTHON_BINARY,
        "-I",
        "-c",
        fixture.runner.tracked["deploy/scripts/storage_preflight.py"].payload.decode(),
        "--mode",
        "startup",
    ) in fixture.runner.calls
    storage_command = next(
        call
        for call in fixture.runner.calls
        if call[:3] == (fixture.module.PYTHON_BINARY, "-I", "-c")
    )
    assert str(fixture.source_root / "deploy/scripts/storage_preflight.py") not in storage_command
    for unit in ("docker.service", "docker.socket", "containerd.service"):
        assert (
            fixture.module.SYSTEMCTL_BINARY,
            "show",
            "--property=LoadState",
            "--value",
            unit,
        ) in fixture.runner.calls
        assert (fixture.module.SYSTEMCTL_BINARY, "is-active", unit) in fixture.runner.calls
    assert (
        fixture.module.SYSTEMCTL_BINARY,
        "is-active",
        "sms-platform.service",
    ) in fixture.runner.calls
    assert (
        fixture.module.FINDMNT_BINARY,
        "--noheadings",
        "--output",
        "FSTYPE",
        "--target",
        str(fixture.docker_root),
    ) in fixture.runner.calls
    assert fixture.runner.git_sudo_uids
    assert set(fixture.runner.git_sudo_uids) == {str(fixture.source_root.lstat().st_uid)}


def test_plan_rejects_dirty_or_wrong_commit_checkout(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.runner.dirty = True
    with pytest.raises(fixture.module.HostAssetInstallError, match="clean expected commit"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]

    fixture.runner.dirty = False
    with pytest.raises(fixture.module.HostAssetInstallError, match="expected commit"):
        fixture.installer.plan("A" * 40)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "raw_sudo_uid",
    (None, "", "+501", " 501", "501 ", "0501", "uid501", str(2**32 - 1), "502"),
)
def test_sudo_uid_must_be_canonical_decimal_and_match_checkout_owner(
    raw_sudo_uid: str | None,
) -> None:
    module = installer_module()
    with pytest.raises(module.HostAssetInstallError, match="ownership is not trusted"):
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=0,
            raw_sudo_uid=raw_sudo_uid,
        )


def test_sudo_uid_is_used_only_for_matching_non_root_checkout() -> None:
    module = installer_module()
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=0,
            raw_sudo_uid="501",
        )
        == "501"
    )
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=0,
            effective_uid=0,
            raw_sudo_uid="untrusted-value",
        )
        is None
    )
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=501,
            raw_sudo_uid="untrusted-value",
        )
        is None
    )


def test_plan_rejects_sudo_uid_mismatch_before_running_git(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.installer.raw_sudo_uid = str(fixture.source_root.lstat().st_uid + 1)  # type: ignore[attr-defined]
    with pytest.raises(fixture.module.HostAssetInstallError, match="ownership is not trusted"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]
    assert fixture.runner.git_sudo_uids == []


def test_plan_rejects_source_bytes_or_source_symlink_not_in_commit(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    source = fixture.source_root / "deploy/systemd/compose.env.example"
    source.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(fixture.module.HostAssetInstallError, match="bytes"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]

    source.unlink()
    source.symlink_to(fixture.source_root / "deploy/sms-compose")
    with pytest.raises(fixture.module.HostAssetInstallError, match="regular file"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]


def test_apply_requires_root_and_both_explicit_confirmations(tmp_path: Path) -> None:
    not_root = make_fixture(tmp_path / "not-root", effective_uid=1234)
    with pytest.raises(not_root.module.HostAssetInstallError, match="requires root"):
        apply(not_root)

    fixture = make_fixture(tmp_path / "confirm")
    with pytest.raises(fixture.module.HostAssetInstallError, match="both"):
        fixture.installer.apply(  # type: ignore[union-attr]
            COMMIT,
            confirm_dedicated_production_host=True,
            confirm_vcenter_storage_reviewed=False,
        )
    assert not fixture.etc_root.exists()


def test_apply_blocks_failed_storage_or_active_docker_without_writes(
    tmp_path: Path,
) -> None:
    storage = make_fixture(tmp_path / "storage")
    storage.runner.storage_passed = False
    with pytest.raises(storage.module.HostAssetInstallError, match="storage preflight"):
        apply(storage)
    assert not storage.etc_root.exists()

    active = make_fixture(tmp_path / "active")
    active.runner.active_service = "docker.service"
    with pytest.raises(active.module.HostAssetInstallError, match="inactive"):
        apply(active)
    assert not active.etc_root.exists()


def test_service_contract_accepts_masked_docker_and_absent_platform_only(
    tmp_path: Path,
) -> None:
    masked = make_fixture(tmp_path / "masked")
    masked.runner.platform_absent_returncode = 3
    masked.runner.platform_absent_active_state = "inactive"
    assert masked.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]

    loaded_platform = make_fixture(tmp_path / "loaded-platform")
    loaded_platform.runner.platform_load_state = "loaded"
    with pytest.raises(loaded_platform.module.HostAssetInstallError, match="unit must be absent"):
        loaded_platform.installer.plan(COMMIT)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("unit", "fault"),
    tuple(
        (unit, fault)
        for unit in ("docker.service", "docker.socket", "containerd.service")
        for fault in ("not-masked", "active")
    ),
)
def test_each_docker_unit_must_be_masked_and_inactive_before_any_write(
    tmp_path: Path,
    unit: str,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    if fault == "not-masked":
        fixture.runner.docker_load_states[unit] = "loaded"
    else:
        fixture.runner.active_service = unit
    with pytest.raises(fixture.module.HostAssetInstallError, match="masked and inactive"):
        apply(fixture)
    assert not fixture.etc_root.exists()


def test_empty_xfs_and_exact_ext4_lost_found_are_the_only_allowed_docker_roots(
    tmp_path: Path,
) -> None:
    xfs = make_fixture(tmp_path / "xfs")
    xfs.runner.docker_filesystem = "xfs"
    assert xfs.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]

    ext4 = make_fixture(tmp_path / "ext4")
    lost_found = ext4.docker_root / "lost+found"
    lost_found.mkdir(mode=0o700)
    lost_found.chmod(0o700)
    assert ext4.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "fault",
    ("xfs-lost-found", "extra-file", "unsafe-lost-found", "recovered-entry"),
)
def test_nonempty_or_unsafe_docker_root_blocks_before_any_write(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    if fault == "xfs-lost-found":
        fixture.runner.docker_filesystem = "xfs"
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
    elif fault == "extra-file":
        (fixture.docker_root / "docker-state").write_text("unexpected\n", encoding="utf-8")
    elif fault == "unsafe-lost-found":
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    else:
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        (target / "recovered-data").write_text("not empty\n", encoding="utf-8")
    with pytest.raises(fixture.module.HostAssetInstallError, match="Docker root"):
        apply(fixture)
    assert not fixture.etc_root.exists()


def test_docker_root_symlink_or_wrong_mode_blocks_before_any_write(tmp_path: Path) -> None:
    linked = make_fixture(tmp_path / "linked")
    linked.docker_root.rmdir()
    actual = linked.docker_root.parent / "actual-docker"
    actual.mkdir(mode=0o710)
    linked.docker_root.symlink_to(actual, target_is_directory=True)
    with pytest.raises(linked.module.HostAssetInstallError, match="symlink"):
        apply(linked)
    assert not linked.etc_root.exists()

    wrong_mode = make_fixture(tmp_path / "wrong-mode")
    wrong_mode.docker_root.chmod(0o755)
    with pytest.raises(wrong_mode.module.HostAssetInstallError, match="Docker root is unsafe"):
        apply(wrong_mode)
    assert not wrong_mode.etc_root.exists()

    former_contract = make_fixture(tmp_path / "former-0711")
    former_contract.docker_root.chmod(0o711)
    with pytest.raises(
        former_contract.module.HostAssetInstallError, match="Docker root is unsafe"
    ):
        apply(former_contract)
    assert not former_contract.etc_root.exists()


def test_apply_installs_exact_modes_symlink_and_commits_state_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    events: list[Path] = []
    real_regular = fixture.module._commit_regular_no_replace
    real_symlink = fixture.module._commit_symlink_no_replace

    def record_regular(temporary: Path, destination: Path) -> None:
        events.append(destination)
        real_regular(temporary, destination)

    def record_symlink(spec, *, expected_uid: int, expected_gid: int) -> None:  # type: ignore[no-untyped-def]
        events.append(spec.destination)
        real_symlink(spec, expected_uid=expected_uid, expected_gid=expected_gid)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", record_regular)
    monkeypatch.setattr(fixture.module, "_commit_symlink_no_replace", record_symlink)

    result = apply(fixture)

    assert result["status"] == "installed"
    assert events[-1] == fixture.state_path
    assert fixture.state_path.is_file()
    state = json.loads(fixture.state_path.read_text(encoding="ascii"))
    assert state["source_commit"] == COMMIT
    assert len(state["assets"]) == 18
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        metadata = spec.destination.lstat()
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_gid == os.getegid()
        if spec.kind == "regular":
            assert stat.S_IMODE(metadata.st_mode) == spec.mode
            assert (
                spec.destination.read_bytes()
                == (fixture.source_root / spec.source_relative).read_bytes()
            )
        else:
            assert spec.destination.is_symlink()
            assert os.readlink(spec.destination) == str(spec.symlink_target)
    assert stat.S_IMODE(fixture.state_path.stat().st_mode) == 0o600
    assert fixture.installer.status()["status"] == "installed"  # type: ignore[union-attr]


def test_apply_never_invokes_mutating_host_or_git_commands(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    apply(fixture)

    flattened = [" ".join(call) for call in fixture.runner.calls]
    assert not any(" fetch" in command for command in flattened)
    assert not any(" apt" in command or "mkfs" in command for command in flattened)
    assert not any(" mount" in command or "fstab" in command for command in flattened)
    assert not any("docker compose" in command for command in flattened)
    systemctl_calls = [
        call for call in fixture.runner.calls if call[0] == fixture.module.SYSTEMCTL_BINARY
    ]
    assert systemctl_calls
    assert all(call[1] in {"is-active", "show"} for call in systemctl_calls)


def test_apply_refuses_any_existing_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.etc_root.mkdir(mode=0o700)
    target = fixture.etc_root / "compose.env"
    target.write_text("operator-owned\n", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(fixture.module.HostAssetInstallError, match="already exists"):
        apply(fixture)

    assert target.read_text(encoding="utf-8") == "operator-owned\n"
    assert not fixture.state_path.exists()


def test_failed_publish_never_commits_state_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_commit = fixture.module._commit_regular_no_replace
    calls = 0

    def fail_second(temporary: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fixture.module.HostAssetInstallError("simulated publish failure")
        real_commit(temporary, destination)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", fail_second)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated"):
        apply(fixture)

    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "installing"  # type: ignore[union-attr]


def test_wrapper_target_drift_before_state_prevents_success_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_symlink = fixture.module._commit_symlink_no_replace

    def publish_then_drift(spec, *, expected_uid: int, expected_gid: int) -> None:  # type: ignore[no-untyped-def]
        real_symlink(spec, expected_uid=expected_uid, expected_gid=expected_gid)
        spec.symlink_target.write_text("drift after symlink\n", encoding="utf-8")
        spec.symlink_target.chmod(0o755)

    monkeypatch.setattr(fixture.module, "_commit_symlink_no_replace", publish_then_drift)
    with pytest.raises(fixture.module.HostAssetInstallError, match="wrapper target has drifted"):
        apply(fixture)
    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "installing"  # type: ignore[union-attr]


def test_post_state_drift_never_returns_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_commit = fixture.module._commit_regular_no_replace

    def publish_then_drift(temporary: Path, destination: Path) -> None:
        real_commit(temporary, destination)
        if destination == fixture.state_path:
            target = fixture.source_root / "deploy/sms-compose"
            target.write_text("drift after state\n", encoding="utf-8")
            target.chmod(0o755)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", publish_then_drift)
    with pytest.raises(fixture.module.HostAssetInstallError, match="post-publish"):
        apply(fixture)
    assert fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


def test_status_is_read_only_for_absent_incomplete_and_drifted_states(
    tmp_path: Path,
) -> None:
    absent = make_fixture(tmp_path / "absent")
    assert absent.installer.status() == {  # type: ignore[union-attr]
        "action": "status",
        "assets_present": 0,
        "status": "absent",
    }

    installed = make_fixture(tmp_path / "installed")
    apply(installed)
    target = installed.etc_root / "compose.env"
    target.write_text("drift\n", encoding="utf-8")
    target.chmod(0o600)
    assert installed.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


@pytest.mark.parametrize("drift", ("content", "missing", "mode"))
def test_status_fails_closed_when_wrapper_target_drifts(
    tmp_path: Path,
    drift: str,
) -> None:
    fixture = make_fixture(tmp_path)
    apply(fixture)
    target = fixture.source_root / "deploy/sms-compose"
    if drift == "content":
        target.write_text("changed wrapper\n", encoding="utf-8")
        target.chmod(0o755)
    elif drift == "missing":
        target.unlink()
    else:
        target.chmod(0o644)

    assert fixture.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


def test_cli_exposes_only_reviewed_arguments() -> None:
    module = installer_module()
    parser = module._parser()
    parsed = parser.parse_args(
        [
            "apply",
            "--expected-commit",
            COMMIT,
            "--confirm-dedicated-production-host",
            "--confirm-vcenter-storage-reviewed",
        ]
    )
    assert parsed.action == "apply"
    resumed = parser.parse_args(["resume", "--expected-commit", COMMIT])
    assert resumed.action == "resume"
    upgraded = parser.parse_args(
        [
            "apply",
            "--expected-commit",
            NEW_COMMIT,
            "--from-commit",
            COMMIT,
        ]
    )
    assert upgraded.from_commit == COMMIT
    accepted = parser.parse_args(
        [
            "upgrade-accept",
            "--expected-commit",
            NEW_COMMIT,
            "--from-commit",
            COMMIT,
        ]
    )
    assert accepted.action == "upgrade-accept"
    rolled = parser.parse_args(
        [
            "rollback",
            "--expected-commit",
            COMMIT,
            "--confirm-rollback-this-install",
        ]
    )
    assert rolled.action == "rollback"
    for forbidden in ("--root", "--force", "--skip-storage", "--source-root"):
        with pytest.raises(SystemExit):
            parser.parse_args(["status", forbidden])


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        ({"action": "plan", "status": "ready"}, 0),
        ({"action": "apply", "status": "installed"}, 0),
        ({"action": "apply", "status": "awaiting_acceptance"}, 0),
        ({"action": "status", "status": "installed"}, 0),
        ({"action": "status", "status": "absent"}, 1),
        ({"action": "status", "status": "incomplete"}, 1),
        ({"action": "status", "status": "installing"}, 1),
        ({"action": "status", "status": "upgrading"}, 1),
        ({"action": "status", "status": "rollback_required"}, 1),
        ({"action": "resume", "status": "installed"}, 0),
        ({"action": "resume", "status": "awaiting_acceptance"}, 0),
        ({"action": "upgrade-accept", "status": "installed"}, 0),
        ({"action": "rollback", "status": "rolled_back"}, 0),
        ({"action": "status", "status": "drifted"}, 1),
    ),
)
def test_cli_exit_code_is_zero_only_for_complete_success(
    result: dict[str, object], expected: int
) -> None:
    module = installer_module()
    assert module._result_exit_code(result) == expected


def test_status_absent_keeps_bounded_json_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = installer_module()

    class AbsentInstaller:
        def status(self) -> dict[str, object]:
            return {"action": "status", "assets_present": 0, "status": "absent"}

    monkeypatch.setattr(module, "ProductionHostAssetInstaller", AbsentInstaller)
    assert module.main(["status"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "action": "status",
        "assets_present": 0,
        "status": "absent",
    }
    assert captured.err == ""


def test_blocked_cli_stderr_never_echoes_internal_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = installer_module()

    class BlockedInstaller:
        def plan(self, _expected_commit: str) -> dict[str, object]:
            raise module.HostAssetInstallError("private path and command detail")

        def resume(self, _expected_commit: str) -> dict[str, object]:
            raise module.HostAssetInstallError("secret=/etc/sms-platform/private")

        def rollback(
            self, _expected_commit: str, *, confirm_rollback_this_install: bool
        ) -> dict[str, object]:
            raise module.HostAssetInstallError("credential=super-secret")

    monkeypatch.setattr(module, "ProductionHostAssetInstaller", BlockedInstaller)
    assert module.main(["plan", "--expected-commit", COMMIT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"action": "plan", "status": "blocked"}
    assert "private" not in captured.err
    assert module.main(["resume", "--expected-commit", COMMIT]) == 1
    resumed = capsys.readouterr()
    assert json.loads(resumed.err) == {"action": "resume", "status": "blocked"}
    assert "secret" not in resumed.err
    assert (
        module.main(
            [
                "rollback",
                "--expected-commit",
                COMMIT,
                "--confirm-rollback-this-install",
            ]
        )
        == 1
    )
    rolled = capsys.readouterr()
    assert json.loads(rolled.err) == {"action": "rollback", "status": "blocked"}
    assert "credential" not in rolled.err
    assert "super-secret" not in rolled.err


def test_subprocess_runner_passes_sudo_uid_only_to_fixed_read_only_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = installer_module()
    environments: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    runner = module.SubprocessRunner()
    git_argv = (
        module.GIT_BINARY,
        "-C",
        "/opt/sms-platform",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    assert runner.run(git_argv, git_sudo_uid="501").returncode == 0
    assert environments[-1]["SUDO_UID"] == "501"

    assert runner.run((module.PYTHON_BINARY, "--version")).returncode == 0
    assert "SUDO_UID" not in environments[-1]
    calls_before = len(environments)
    assert runner.run((module.PYTHON_BINARY, "--version"), git_sudo_uid="501").returncode == 126
    assert len(environments) == calls_before


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    (
        (b"x" * (2 * 1024 * 1024 + 1), b""),
        (b"", b"x" * (2 * 1024 * 1024 + 1)),
        (b"x" * (1024 * 1024 + 1), b"x" * (1024 * 1024 + 1)),
    ),
)
def test_subprocess_runner_rejects_oversized_captured_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
) -> None:
    module = installer_module()

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.SubprocessRunner().run((module.PYTHON_BINARY, "--version"))
    assert result.returncode == module.COMMAND_OUTPUT_LIMIT_RETURN_CODE
    assert result.stdout == b""
    assert result.stderr == b""


def _interrupt_after(fixture: Fixture, monkeypatch: pytest.MonkeyPatch, index: int) -> None:
    real_regular = fixture.module._commit_regular_no_replace
    real_symlink = fixture.module._commit_symlink_no_replace
    published = {"n": 0}

    def fail_regular(temporary: Path, destination: Path) -> None:
        if destination == fixture.state_path:
            real_regular(temporary, destination)
            return
        if published["n"] == index:
            raise fixture.module.HostAssetInstallError("simulated interrupt")
        real_regular(temporary, destination)
        published["n"] += 1

    def fail_symlink(spec, *, expected_uid: int, expected_gid: int) -> None:  # type: ignore[no-untyped-def]
        if published["n"] == index:
            raise fixture.module.HostAssetInstallError("simulated interrupt")
        real_symlink(spec, expected_uid=expected_uid, expected_gid=expected_gid)
        published["n"] += 1

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", fail_regular)
    monkeypatch.setattr(fixture.module, "_commit_symlink_no_replace", fail_symlink)


@pytest.mark.parametrize("index", range(18))
def test_crash_at_each_asset_publish_boundary_can_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
) -> None:
    fixture = make_fixture(tmp_path)
    _interrupt_after(fixture, monkeypatch, index)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        apply(fixture)
    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "installing"  # type: ignore[union-attr]
    monkeypatch.undo()
    result = fixture.installer.resume(COMMIT)  # type: ignore[union-attr]
    assert result["status"] == "installed"
    assert fixture.installer.status()["status"] == "installed"  # type: ignore[union-attr]
    assert fixture.state_path.is_file()


@pytest.mark.parametrize("index", range(18))
def test_crash_at_each_asset_publish_boundary_can_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
) -> None:
    fixture = make_fixture(tmp_path)
    _interrupt_after(fixture, monkeypatch, index)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        apply(fixture)
    extra = fixture.etc_root / "operator-notes"
    extra.write_text("keep-me\n", encoding="utf-8")
    extra.chmod(0o600)
    monkeypatch.undo()
    result = fixture.installer.rollback(  # type: ignore[union-attr]
        COMMIT, confirm_rollback_this_install=True
    )
    assert result["status"] == "rolled_back"
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        assert not fixture.module._lexists(spec.destination)
    assert not fixture.state_path.exists()
    assert not (fixture.etc_root / "production-host-assets.intent.json").exists()
    assert extra.read_text(encoding="utf-8") == "keep-me\n"
    assert apply(fixture)["status"] == "installed"


def test_concurrent_apply_only_one_installer_lock(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.etc_root.mkdir(parents=True, mode=0o700)
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with fixture.installer._exclusive_lock():  # type: ignore[union-attr]
            held.set()
            release.wait(timeout=2)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert held.wait(timeout=2)
    with pytest.raises(fixture.module.HostAssetInstallError, match="lock"):
        apply(fixture)
    release.set()
    worker.join(timeout=2)


def test_matching_partial_destinations_resume_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    _interrupt_after(fixture, monkeypatch, 5)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        apply(fixture)
    monkeypatch.undo()
    assert apply(fixture)["status"] == "installed"
    assert fixture.installer.status()["source_commit"] == COMMIT  # type: ignore[union-attr]


@pytest.mark.parametrize("drift", ("content", "mode"))
def test_mismatched_existing_destination_fails_closed_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    fixture = make_fixture(tmp_path)
    _interrupt_after(fixture, monkeypatch, 3)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        apply(fixture)
    monkeypatch.undo()
    target = fixture.etc_root / "compose.env"
    original = target.read_bytes()
    if drift == "content":
        target.write_text("tampered\n", encoding="utf-8")
        target.chmod(0o600)
    else:
        target.chmod(0o644)
    with pytest.raises(fixture.module.HostAssetInstallError, match="already exists"):
        apply(fixture)
    if drift == "content":
        assert target.read_text(encoding="utf-8") == "tampered\n"
    else:
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert target.read_bytes() == original
    assert not fixture.state_path.exists()


def test_state_publish_failure_is_resumable_without_manual_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_regular = fixture.module._commit_regular_no_replace

    def fail_state(temporary: Path, destination: Path) -> None:
        if destination == fixture.state_path:
            raise fixture.module.HostAssetInstallError("state publish failed")
        real_regular(temporary, destination)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", fail_state)
    with pytest.raises(fixture.module.HostAssetInstallError, match="state publish"):
        apply(fixture)
    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "installing"  # type: ignore[union-attr]
    monkeypatch.undo()
    assert fixture.installer.resume(COMMIT)["status"] == "installed"  # type: ignore[union-attr]


def test_resume_and_rollback_are_reentrant_after_their_own_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    _interrupt_after(fixture, monkeypatch, 4)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        apply(fixture)
    monkeypatch.undo()
    _interrupt_after(fixture, monkeypatch, 8)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated interrupt"):
        fixture.installer.resume(COMMIT)  # type: ignore[union-attr]
    monkeypatch.undo()
    assert fixture.installer.resume(COMMIT)["status"] == "installed"  # type: ignore[union-attr]


@pytest.mark.parametrize("error_no", (errno.ENOSPC, errno.EIO, errno.EACCES))
def test_publish_os_errors_are_resumable_after_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_no: int,
) -> None:
    fixture = make_fixture(tmp_path)
    real_link = os.link
    calls = {"n": 0}

    def flaky_link(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        **kwargs: object,
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(error_no, "injected")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", flaky_link)
    with pytest.raises(fixture.module.HostAssetInstallError):
        apply(fixture)
    monkeypatch.setattr(os, "link", real_link)
    assert apply(fixture)["status"] == "installed"


def test_destinations_without_intent_are_rollback_required(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.etc_root.mkdir(mode=0o700)
    (fixture.etc_root / "compose.env").write_text("operator-owned\n", encoding="utf-8")
    (fixture.etc_root / "compose.env").chmod(0o600)
    assert fixture.installer.status()["status"] == "rollback_required"  # type: ignore[union-attr]


def test_upgrade_plan_from_legacy_v1_state_is_read_only_and_deterministic(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    before = {
        str(path.relative_to(tmp_path)): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }

    result = fixture.installer.plan(  # type: ignore[union-attr]
        NEW_COMMIT, from_commit=COMMIT
    )

    assert result == {
        "action": "plan",
        "assets": 18,
        "assets_changed": 6,
        "changed_assets": list(UPGRADE_NAMES),
        "from_commit": COMMIT,
        "mode": "upgrade",
        "source_commit": NEW_COMMIT,
        "status": "ready",
    }
    after = {
        str(path.relative_to(tmp_path)): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }
    assert after == before


def test_post_first_start_plan_allows_retained_docker_metadata_only_for_fixed_profile(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)

    result = fixture.installer.plan(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT, from_commit=POST_FIRST_START_COMMIT
    )

    assert result == {
        "action": "plan",
        "assets": 18,
        "assets_changed": 1,
        "changed_assets": ["storage-preflight"],
        "from_commit": POST_FIRST_START_COMMIT,
        "mode": "upgrade",
        "source_commit": POST_FIRST_START_TARGET_COMMIT,
        "status": "ready",
    }
    assert (fixture.docker_root / "engine-id").read_text(encoding="utf-8") == ("retained\n")

    legacy = make_fixture(tmp_path / "legacy")
    prepare_upgrade(legacy)
    (legacy.docker_root / "engine-id").write_text("retained\n", encoding="utf-8")
    with pytest.raises(legacy.module.HostAssetInstallError, match="not empty"):
        legacy.installer.plan(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    legacy_mode = make_fixture(tmp_path / "legacy-wrong-0710")
    prepare_upgrade(legacy_mode)
    legacy_mode.docker_root.chmod(0o710)
    with pytest.raises(
        legacy_mode.module.HostAssetInstallError, match="Docker root is unsafe"
    ):
        legacy_mode.installer.plan(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )


def test_post_first_start_profile_requires_safe_0710_docker_root(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)
    fixture.docker_root.chmod(0o711)

    with pytest.raises(fixture.module.HostAssetInstallError, match="Docker root is unsafe"):
        fixture.installer.plan(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT,
            from_commit=POST_FIRST_START_COMMIT,
        )


def test_post_first_start_profile_rejects_every_other_from_commit(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)

    with pytest.raises(fixture.module.HostAssetInstallError, match="fixed source commit"):
        fixture.installer.plan(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT, from_commit="d" * 40
        )
    assert not (
        fixture.etc_root / "production-host-assets.upgrade.intent.json"
    ).exists()


def test_legacy_prebootstrap_profile_rejects_every_other_target_commit(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.runner.head_commit = POST_FIRST_START_TARGET_COMMIT

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="legacy pre-bootstrap repair requires its fixed target commit",
    ):
        fixture.installer.plan(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT, from_commit=COMMIT
        )
    assert not (
        fixture.etc_root / "production-host-assets.upgrade.intent.json"
    ).exists()


@pytest.mark.parametrize("fault", ("extra-byte", "wrong-mode", "extra-asset"))
def test_post_first_start_profile_rejects_any_change_beyond_exact_mode_repair(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)
    if fault in {"extra-byte", "wrong-mode"}:
        spec = next(
            item
            for item in fixture.installer.assets  # type: ignore[union-attr]
            if item.name == "storage-preflight"
        )
        source = fixture.source_root / spec.source_relative
        payload = source.read_bytes()
        if fault == "extra-byte":
            payload += b"# unreviewed\n"
        else:
            payload = payload.replace(b"0o710", b"0o700", 1)
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode,
            payload=payload,
        )
    else:
        spec = next(
            item
            for item in fixture.installer.assets  # type: ignore[union-attr]
            if item.name == "compose-env"
        )
        source = fixture.source_root / spec.source_relative
        payload = source.read_bytes() + b"unreviewed\n"
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode,
            payload=payload,
        )

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="only fix|exactly storage-preflight",
    ):
        fixture.installer.plan(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT,
            from_commit=POST_FIRST_START_COMMIT,
        )


def test_post_first_start_apply_accepts_fresh_preflight_then_commits_state_last(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    old_payload = prepare_post_first_start_repair(fixture)

    applied = fixture.installer.apply(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT,
        from_commit=POST_FIRST_START_COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )

    assert applied["status"] == "awaiting_acceptance"
    assert applied["changed_assets"] == ["storage-preflight"]
    state_before = json.loads(fixture.state_path.read_text(encoding="ascii"))
    assert state_before["source_commit"] == POST_FIRST_START_COMMIT
    intent = json.loads(
        (fixture.etc_root / "production-host-assets.upgrade.intent.json").read_text(
            encoding="ascii"
        )
    )
    assert intent["changes"] == ["storage-preflight"]
    assert intent["backups"]["storage-preflight"]
    assert old_payload != next(
        spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name == "storage-preflight"
    )

    set_upgrade_acceptance_evidence(fixture)
    accepted = fixture.installer.upgrade_accept(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT, from_commit=POST_FIRST_START_COMMIT
    )

    assert accepted["status"] == "installed"
    assert fixture.runner.storage_start_attempted
    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == (
        POST_FIRST_START_TARGET_COMMIT
    )
    assert not (fixture.etc_root / "production-host-assets.upgrade.intent.json").exists()
    assert (fixture.docker_root / "engine-id").exists()


def test_post_first_start_accept_rejects_unverified_unchanged_storage_unit(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT,
        from_commit=POST_FIRST_START_COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="candidate systemd unit fragment path is not loaded",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT,
            from_commit=POST_FIRST_START_COMMIT,
        )

    assert not fixture.runner.storage_start_attempted
    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == (
        POST_FIRST_START_COMMIT
    )


def test_post_first_start_accept_rejects_asset_drift_before_start(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_post_first_start_repair(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT,
        from_commit=POST_FIRST_START_COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    storage_unit = next(
        spec.destination
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name == "storage-unit"
    )
    storage_unit.write_text("drifted before acceptance\n", encoding="utf-8")
    storage_unit.chmod(0o644)

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="unchanged host asset has drifted",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            POST_FIRST_START_TARGET_COMMIT,
            from_commit=POST_FIRST_START_COMMIT,
        )

    assert not fixture.runner.storage_start_attempted
    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == (
        POST_FIRST_START_COMMIT
    )


def test_post_first_start_rollback_restores_only_storage_preflight(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    old_payload = prepare_post_first_start_repair(fixture)
    unchanged_before = {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.kind == "regular" and spec.name != "storage-preflight"
    }
    fixture.installer.apply(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT,
        from_commit=POST_FIRST_START_COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )

    rolled = fixture.installer.rollback(  # type: ignore[union-attr]
        POST_FIRST_START_TARGET_COMMIT,
        from_commit=POST_FIRST_START_COMMIT,
        confirm_rollback_this_install=True,
    )

    storage = next(
        spec
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name == "storage-preflight"
    )
    assert rolled["status"] == "rolled_back"
    assert rolled["assets_restored"] == 1
    assert storage.destination.read_bytes() == old_payload
    assert {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.kind == "regular" and spec.name != "storage-preflight"
    } == unchanged_before
    assert fixture.installer.status()["source_commit"] == POST_FIRST_START_COMMIT  # type: ignore[union-attr]
    assert (fixture.docker_root / "engine-id").exists()


def test_upgrade_apply_waits_for_acceptance_then_commits_new_state(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)

    applied = fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )

    assert applied["status"] == "awaiting_acceptance"
    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == COMMIT
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]
    intent = json.loads(
        (fixture.etc_root / "production-host-assets.upgrade.intent.json").read_text(
            encoding="ascii"
        )
    )
    assert intent["schema_version"] == 1
    assert intent["storage_invocation_id_before"] == "1" * 32
    changed_specs = [
        spec
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    ]
    for spec in changed_specs:
        assert (
            spec.destination.read_bytes()
            == (fixture.source_root / spec.source_relative).read_bytes()
        )
    set_upgrade_acceptance_evidence(fixture)

    accepted = fixture.installer.upgrade_accept(  # type: ignore[union-attr]
        NEW_COMMIT, from_commit=COMMIT
    )

    assert accepted["status"] == "installed"
    assert fixture.installer.status()["source_commit"] == NEW_COMMIT  # type: ignore[union-attr]
    assert not (fixture.etc_root / "production-host-assets.upgrade.intent.json").exists()
    assert not (fixture.etc_root / "production-host-assets.upgrade").exists()
    assert (
        fixture.module.SYSTEMCTL_BINARY,
        "--system",
        "--no-ask-password",
        "--job-mode=fail",
        "start",
        "sms-storage-preflight.service",
    ) in fixture.runner.calls
    invocation_query = (
        fixture.module.SYSTEMCTL_BINARY,
        "show",
        "--property=InvocationID",
        "--value",
        "sms-storage-preflight.service",
    )
    assert fixture.runner.calls.count(invocation_query) == 1


def test_upgrade_accept_rejects_stale_success_when_fresh_start_fails(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    fixture.runner.storage_result = "success"
    fixture.runner.storage_exec_main_status = "0"
    fixture.runner.storage_start_returncode = 1

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="did not start successfully",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == COMMIT
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]


def test_upgrade_accept_timeout_preserves_old_state_and_v1_intent(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    intent_path = fixture.etc_root / "production-host-assets.upgrade.intent.json"
    state_before = fixture.state_path.read_bytes()
    intent_before = intent_path.read_bytes()
    fixture.runner.storage_start_returncode = 124
    fixture.runner.storage_state_after = (
        "activating",
        "start-pre",
        "[99 /org/freedesktop/systemd1/job/99 start waiting]",
    )

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="did not start successfully",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    assert fixture.state_path.read_bytes() == state_before
    assert intent_path.read_bytes() == intent_before
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]
    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="must be quiescent",
    ):
        fixture.installer.rollback(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_rollback_this_install=True,
        )
    assert fixture.state_path.read_bytes() == state_before
    assert intent_path.read_bytes() == intent_before


@pytest.mark.parametrize(
    ("result", "exec_status"),
    (("", "0"), ("success", ""), ("failed", "0"), ("success", "1")),
)
def test_upgrade_accept_rejects_missing_or_failed_fresh_result(
    tmp_path: Path,
    result: str,
    exec_status: str,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    fixture.runner.storage_result = result
    fixture.runner.storage_exec_main_status = exec_status

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="has not completed successfully",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]


def test_upgrade_accept_allows_failed_prior_state_but_requires_inactive_after_start(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    fixture.runner.storage_state_before = ("failed", "failed", "")

    accepted = fixture.installer.upgrade_accept(  # type: ignore[union-attr]
        NEW_COMMIT, from_commit=COMMIT
    )

    assert accepted["status"] == "installed"


def test_upgrade_accept_rejects_noninactive_state_after_fresh_start(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    fixture.runner.storage_state_after = ("active", "running", "")

    with pytest.raises(
        fixture.module.HostAssetInstallError,
        match="must be quiescent",
    ):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]


@pytest.mark.parametrize("index", range(len(UPGRADE_NAMES)))
def test_upgrade_replace_crash_resumes_without_manual_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    real_replace = fixture.module._commit_regular_replace
    published = 0
    destinations = {
        spec.destination
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    }

    def replace_then_interrupt(temporary: Path, destination: Path) -> None:
        nonlocal published
        real_replace(temporary, destination)
        if destination in destinations:
            if published == index:
                raise fixture.module.HostAssetInstallError("upgrade interrupt")
            published += 1

    monkeypatch.setattr(fixture.module, "_commit_regular_replace", replace_then_interrupt)
    with pytest.raises(fixture.module.HostAssetInstallError, match="upgrade interrupt"):
        fixture.installer.apply(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_dedicated_production_host=True,
            confirm_vcenter_storage_reviewed=True,
        )
    monkeypatch.undo()

    resumed = fixture.installer.resume(  # type: ignore[union-attr]
        NEW_COMMIT, from_commit=COMMIT
    )
    assert resumed["status"] == "awaiting_acceptance"
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]


def test_partial_upgrade_can_rollback_exact_legacy_state(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    old_state = fixture.state_path.read_bytes()
    old_assets = {
        spec.name: (spec.destination.read_bytes() if spec.kind == "regular" else None)
        for spec in fixture.installer.assets  # type: ignore[union-attr]
    }
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )

    rolled = fixture.installer.rollback(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_rollback_this_install=True,
    )

    assert rolled["status"] == "rolled_back"
    assert fixture.state_path.read_bytes() == old_state
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        if spec.kind == "regular":
            assert spec.destination.read_bytes() == old_assets[spec.name]
    assert fixture.installer.status()["source_commit"] == COMMIT  # type: ignore[union-attr]


def test_upgrade_rollback_rechecks_mutable_boundary_before_restoring_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    intent_path = fixture.etc_root / "production-host-assets.upgrade.intent.json"
    state_before = fixture.state_path.read_bytes()
    intent_before = intent_path.read_bytes()
    assets_before = {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    }
    real_inspect = fixture.installer._inspect_upgrade_readiness  # type: ignore[union-attr]

    def inspect_then_cross_boundary(  # type: ignore[no-untyped-def]
        expected_commit: str, *, from_commit: str
    ):
        snapshots = real_inspect(expected_commit, from_commit=from_commit)
        fixture.runner.active_service = "sms-backup.service"
        return snapshots

    monkeypatch.setattr(
        fixture.installer, "_inspect_upgrade_readiness", inspect_then_cross_boundary
    )
    with pytest.raises(fixture.module.HostAssetInstallError, match="maintenance services"):
        fixture.installer.rollback(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_rollback_this_install=True,
        )

    assert fixture.state_path.read_bytes() == state_before
    assert intent_path.read_bytes() == intent_before
    assert {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    } == assets_before


@pytest.mark.parametrize("index", range(len(UPGRADE_NAMES)))
def test_upgrade_rollback_replace_crash_converges_on_next_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    old_assets = {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    }
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    real_replace = fixture.module._commit_regular_replace
    restored = 0
    destinations = {
        spec.destination
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    }

    def replace_then_interrupt(temporary: Path, destination: Path) -> None:
        nonlocal restored
        real_replace(temporary, destination)
        if destination in destinations:
            if restored == index:
                raise fixture.module.HostAssetInstallError("rollback interrupt")
            restored += 1

    monkeypatch.setattr(fixture.module, "_commit_regular_replace", replace_then_interrupt)
    with pytest.raises(fixture.module.HostAssetInstallError, match="rollback interrupt"):
        fixture.installer.rollback(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_rollback_this_install=True,
        )
    monkeypatch.undo()

    result = fixture.installer.rollback(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_rollback_this_install=True,
    )
    assert result["status"] == "rolled_back"
    assert fixture.installer.status()["source_commit"] == COMMIT  # type: ignore[union-attr]
    assert {
        spec.name: spec.destination.read_bytes()
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name in UPGRADE_NAMES
    } == old_assets


def test_upgrade_rejects_wrapper_change_and_enabled_timer(tmp_path: Path) -> None:
    wrapper = make_fixture(tmp_path / "wrapper")
    prepare_upgrade(wrapper)
    spec = next(
        item
        for item in wrapper.installer.assets  # type: ignore[union-attr]
        if item.name == "compose-wrapper"
    )
    payload = b"changed wrapper\n"
    source = wrapper.source_root / spec.source_relative
    source.write_bytes(payload)
    source.chmod(0o755)
    wrapper.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
        mode=spec.git_mode, payload=payload
    )
    with pytest.raises(wrapper.module.HostAssetInstallError, match="drifted|wrapper"):
        wrapper.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]

    enabled = make_fixture(tmp_path / "enabled")
    prepare_upgrade(enabled)
    enabled.runner.enabled_states["sms-backup.timer"] = (0, "enabled")
    with pytest.raises(enabled.module.HostAssetInstallError, match="pre-bootstrap"):
        enabled.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]
    assert not (enabled.etc_root / "production-host-assets.upgrade.intent.json").exists()


@pytest.mark.parametrize("scope", ("missing", "extra", "zero"))
def test_upgrade_scope_is_exactly_the_reviewed_six_assets(tmp_path: Path, scope: str) -> None:
    fixture = make_fixture(tmp_path)
    seed_legacy_v1_install(fixture)
    if scope == "missing":
        checkout_upgrade_target(fixture, changed_names=UPGRADE_NAMES[:-1])
    elif scope == "extra":
        checkout_upgrade_target(fixture)
        spec = next(
            item
            for item in fixture.installer.assets  # type: ignore[union-attr]
            if item.name == "compose-env"
        )
        source = fixture.source_root / spec.source_relative
        payload = source.read_bytes() + b"unreviewed change\n"
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode, payload=payload
        )
    else:
        fixture.runner.head_commit = NEW_COMMIT

    with pytest.raises(fixture.module.HostAssetInstallError, match="changed|six"):
        fixture.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]
    assert not (fixture.etc_root / "production-host-assets.upgrade.intent.json").exists()


@pytest.mark.parametrize("fault", ("ptrace", "credential", "marker", "script"))
def test_upgrade_rejects_non_zero_capability_or_incomplete_credential_contract(
    tmp_path: Path, fault: str
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    name = "storage-unit" if fault != "script" else "storage-preflight"
    spec = next(
        item
        for item in fixture.installer.assets  # type: ignore[union-attr]
        if item.name == name
    )
    source = fixture.source_root / spec.source_relative
    payload = source.read_bytes()
    if fault == "ptrace":
        payload = payload.replace(
            b"CapabilityBoundingSet=\n",
            b"CapabilityBoundingSet=CAP_SYS_PTRACE\n",
        )
    elif fault == "credential":
        payload = payload.replace(b"LoadCredential=sms-host-mountinfo:/proc/1/mountinfo\n", b"")
    elif fault == "marker":
        payload = payload.replace(b"Environment=SMS_STORAGE_HOST_MOUNTINFO_CREDENTIAL=1\n", b"")
    else:
        payload = payload.replace(b"CREDENTIALS_DIRECTORY", b"OTHER_DIRECTORY")
    source.write_bytes(payload)
    source.chmod(int(spec.git_mode[-3:], 8))
    fixture.runner.tracked[spec.source_relative.as_posix()] = TrackedAsset(
        mode=spec.git_mode, payload=payload
    )

    with pytest.raises(fixture.module.HostAssetInstallError, match="credential|capability"):
        fixture.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]


def test_upgrade_prebootstrap_blocks_active_maintenance_or_release_state(
    tmp_path: Path,
) -> None:
    active = make_fixture(tmp_path / "active")
    prepare_upgrade(active)
    active.runner.active_service = "sms-backup.service"
    with pytest.raises(active.module.HostAssetInstallError, match="maintenance services"):
        active.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]

    release = make_fixture(tmp_path / "release")
    prepare_upgrade(release)
    release.releases_root.mkdir(parents=True, mode=0o700)
    release.releases_root.chmod(0o700)
    (release.releases_root / "prepared-release").mkdir()
    with pytest.raises(release.module.HostAssetInstallError, match="release root"):
        release.installer.plan(NEW_COMMIT, from_commit=COMMIT)  # type: ignore[union-attr]


@pytest.mark.parametrize("fault", ("backup", "destination"))
def test_upgrade_status_reports_drift_for_transaction_tampering(tmp_path: Path, fault: str) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    if fault == "backup":
        target = fixture.etc_root / "production-host-assets.upgrade.intent.json"
        intent = json.loads(target.read_text(encoding="ascii"))
        intent["backups"]["storage-unit"] = "dGFtcGVyZWQK"
        target.write_text(
            json.dumps(intent, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="ascii",
        )
        target.chmod(0o600)
    else:
        target = next(
            spec.destination
            for spec in fixture.installer.assets  # type: ignore[union-attr]
            if spec.name == "storage-unit"
        )
        target.write_text("third version\n", encoding="utf-8")
        target.chmod(0o644)
    assert fixture.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("property_name", "value"),
    (
        ("NeedDaemonReload", "yes"),
        ("Environment", "OTHER=1"),
        ("FragmentPath", "/run/systemd/transient/sms-storage-preflight.service"),
    ),
)
def test_upgrade_accept_rejects_unloaded_systemd_contract(
    tmp_path: Path, property_name: str, value: str
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    unit = "sms-storage-preflight.service"
    if property_name == "NeedDaemonReload":
        fixture.runner.need_daemon_reload[unit] = value
    elif property_name == "FragmentPath":
        fixture.runner.loaded_fragment_paths[unit] = value
    else:
        fixture.runner.loaded_environments[unit] = value

    with pytest.raises(fixture.module.HostAssetInstallError, match="daemon|credential|fragment"):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )
    assert (
        fixture.module.SYSTEMCTL_BINARY,
        "--system",
        "--no-ask-password",
        "--job-mode=fail",
        "start",
        "sms-storage-preflight.service",
    ) not in fixture.runner.calls
    assert json.loads(fixture.state_path.read_text(encoding="ascii"))["source_commit"] == COMMIT


def test_upgrade_accept_rechecks_assets_after_runtime_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)
    intent_path = fixture.etc_root / "production-host-assets.upgrade.intent.json"
    state_before = fixture.state_path.read_bytes()
    intent_before = intent_path.read_bytes()
    target = next(
        spec.destination
        for spec in fixture.installer.assets  # type: ignore[union-attr]
        if spec.name == "storage-unit"
    )
    real_run = fixture.runner.run

    def run_then_drift(  # type: ignore[no-untyped-def]
        argv: tuple[str, ...],
        *,
        git_sudo_uid: str | None = None,
        timeout_seconds: int = 30,
    ):
        result = real_run(
            argv,
            git_sudo_uid=git_sudo_uid,
            timeout_seconds=timeout_seconds,
        )
        if argv[:4] == (
            fixture.module.SYSTEMCTL_BINARY,
            "show",
            "--property=ExecMainStatus",
            "--value",
        ):
            target.write_text("drifted during acceptance\n", encoding="utf-8")
            target.chmod(0o644)
        return result

    monkeypatch.setattr(fixture.runner, "run", run_then_drift)
    with pytest.raises(fixture.module.HostAssetInstallError, match="target assets.*drifted"):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )

    assert fixture.state_path.read_bytes() == state_before
    assert intent_path.read_bytes() == intent_before


def test_accept_state_commit_crash_is_cleaned_by_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    fixture.installer.apply(  # type: ignore[union-attr]
        NEW_COMMIT,
        from_commit=COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )
    set_upgrade_acceptance_evidence(fixture)

    def interrupt_cleanup() -> None:
        raise fixture.module.HostAssetInstallError("cleanup interrupt")

    monkeypatch.setattr(fixture.installer, "_clear_upgrade_transaction", interrupt_cleanup)
    with pytest.raises(fixture.module.HostAssetInstallError, match="cleanup interrupt"):
        fixture.installer.upgrade_accept(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]
    monkeypatch.undo()

    result = fixture.installer.resume(  # type: ignore[union-attr]
        NEW_COMMIT, from_commit=COMMIT
    )
    assert result["status"] == "installed"
    assert fixture.installer.status()["source_commit"] == NEW_COMMIT  # type: ignore[union-attr]


@pytest.mark.parametrize("fault", ("before-link", "after-link"))
def test_upgrade_intent_publish_crash_does_not_block_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    prepare_upgrade(fixture)
    real_commit = fixture.module._commit_regular_no_replace
    intent_path = fixture.etc_root / "production-host-assets.upgrade.intent.json"

    def interrupt_intent_publish(temporary: Path, destination: Path) -> None:
        if destination != intent_path:
            real_commit(temporary, destination)
            return
        if fault == "after-link":
            os.link(temporary, destination, follow_symlinks=False)
        raise fixture.module.HostAssetInstallError("intent publish interrupt")

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", interrupt_intent_publish)
    with pytest.raises(fixture.module.HostAssetInstallError, match="intent publish interrupt"):
        fixture.installer.apply(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_dedicated_production_host=True,
            confirm_vcenter_storage_reviewed=True,
        )
    assert list(fixture.etc_root.glob(f".{intent_path.name}.*.tmp"))
    monkeypatch.undo()

    if fault == "before-link":
        result = fixture.installer.apply(  # type: ignore[union-attr]
            NEW_COMMIT,
            from_commit=COMMIT,
            confirm_dedicated_production_host=True,
            confirm_vcenter_storage_reviewed=True,
        )
    else:
        result = fixture.installer.resume(  # type: ignore[union-attr]
            NEW_COMMIT, from_commit=COMMIT
        )
    assert result["status"] == "awaiting_acceptance"
    assert fixture.installer.status()["status"] == "upgrading"  # type: ignore[union-attr]
