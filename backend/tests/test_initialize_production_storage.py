from __future__ import annotations

import copy
import importlib
import io
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/scripts/initialize_production_storage.py"
sys.path.insert(0, str(SCRIPT.parent))


def initializer_module() -> ModuleType:
    return importlib.import_module("initialize_production_storage")


NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
UUIDS = {
    "docker": "11111111-1111-4111-8111-111111111111",
    "postgres": "22222222-2222-4222-8222-222222222222",
    "redis": "33333333-3333-4333-8333-333333333333",
    "runtime": "44444444-4444-4444-8444-444444444444",
}


def manifest_object(*, not_after: datetime | None = None) -> dict[str, object]:
    devices: dict[str, object] = {
        "os": {
            "by_id": "/dev/disk/by-id/scsi-os-serial",
            "expected_serial": "os-serial",
        }
    }
    for role in ("docker", "postgres", "redis", "runtime"):
        devices[role] = {
            "by_id": f"/dev/disk/by-id/scsi-{role}-serial",
            "expected_serial": f"{role}-serial",
            "filesystem_uuid": UUIDS[role],
        }
    return {
        "change_id": "CHG-20260825-001",
        "devices": devices,
        "not_after": (not_after or NOW + timedelta(hours=8)).isoformat(),
        "reviewer": "infra-reviewer",
        "schema_version": 1,
    }


def parsed_manifest(module: ModuleType):  # type: ignore[no-untyped-def]
    payload = json.dumps(
        manifest_object(), separators=(",", ":"), sort_keys=True
    ).encode()
    return module.parse_manifest(payload, now=NOW)


def observation(module: ModuleType, role: str, index: int):  # type: ignore[no-untyped-def]
    spec = module.SPECS_BY_ROLE[role]
    return module.DeviceObservation(
        role=role,
        by_id=Path(f"/dev/disk/by-id/scsi-{role}-serial"),
        resolved_path=Path(f"/dev/sd{chr(ord('a') + index)}"),
        size_bytes=spec.nominal_bytes,
        serial=f"{role}-serial",
        wwn=f"wwn-{role}",
        major_minor=f"8:{index * 16}",
        filesystem="" if role == "os" else "xfs",
        label="" if role == "os" else spec.label,
        filesystem_uuid="" if role == "os" else UUIDS[role],
        mountpoints=() if role == "os" else (str(spec.mount_path),),
        identity_sha256=f"{index + 1:064x}",
    )


def test_fixed_production_storage_contract_is_exact() -> None:
    module = initializer_module()

    assert [
        (spec.role, spec.nominal_gib, str(spec.mount_path), spec.mount_mode, spec.label)
        for spec in module.ROLE_SPECS
    ] == [
        ("os", 100, "/", 0o755, None),
        ("docker", 250, "/var/lib/docker", 0o711, "sms_docker"),
        ("postgres", 400, "/var/lib/sms-platform/postgres", 0o750, "sms_pg"),
        ("redis", 100, "/var/lib/sms-platform/redis", 0o750, "sms_redis"),
        ("runtime", 200, "/var/lib/sms-platform/runtime", 0o750, "sms_runtime"),
    ]
    directories = [
        directory.path.as_posix()
        for spec in module.DATA_SPECS
        for directory in spec.directories
    ]
    assert directories == [
        "/var/lib/sms-platform/postgres/pgdata",
        "/var/lib/sms-platform/redis/broker",
        "/var/lib/sms-platform/redis/auth",
        "/var/lib/sms-platform/redis/control",
        "/var/lib/sms-platform/runtime/imports",
        "/var/lib/sms-platform/runtime/exports",
        "/var/lib/sms-platform/runtime/raw-spill",
        "/var/lib/sms-platform/runtime/backups",
    ]
    assert [
        (
            bind.role,
            str(bind.source_path),
            str(bind.target_path),
            str(bind.filesystem_root),
        )
        for bind in module.DOCKER_BIND_MOUNT_SPECS
    ] == [
        (
            "postgres",
            "/var/lib/sms-platform/postgres/pgdata",
            "/var/lib/docker/volumes/sms-platform_pgdata/_data",
            "/pgdata",
        ),
        (
            "redis",
            "/var/lib/sms-platform/redis/broker",
            "/var/lib/docker/volumes/sms-platform_redisdata/_data",
            "/broker",
        ),
        (
            "redis",
            "/var/lib/sms-platform/redis/auth",
            "/var/lib/docker/volumes/sms-platform_redisauthdata/_data",
            "/auth",
        ),
        (
            "redis",
            "/var/lib/sms-platform/redis/control",
            "/var/lib/docker/volumes/sms-platform_rediscontroldata/_data",
            "/control",
        ),
        (
            "runtime",
            "/var/lib/sms-platform/runtime/imports",
            "/var/lib/docker/volumes/sms-platform_importdata/_data",
            "/imports",
        ),
        (
            "runtime",
            "/var/lib/sms-platform/runtime/exports",
            "/var/lib/docker/volumes/sms-platform_exportdata/_data",
            "/exports",
        ),
        (
            "runtime",
            "/var/lib/sms-platform/runtime/raw-spill",
            "/var/lib/docker/volumes/sms-platform_rawspill/_data",
            "/raw-spill",
        ),
    ]


def test_manifest_is_closed_expiring_and_binds_preplanned_v4_uuids() -> None:
    module = initializer_module()
    manifest = parsed_manifest(module)

    assert manifest.change_id == "CHG-20260825-001"
    assert manifest.devices["docker"].filesystem_uuid == UUIDS["docker"]
    assert manifest.devices["os"].filesystem_uuid is None

    invalid = manifest_object()
    cast(dict[str, object], invalid["devices"])["docker"] = {
        "by_id": "/dev/sdb",
        "expected_serial": "docker-serial",
        "filesystem_uuid": UUIDS["docker"],
    }
    with pytest.raises(module.StorageInitializationError, match="manifest_by_id_invalid"):
        module.parse_manifest(json.dumps(invalid).encode(), now=NOW)

    expired = manifest_object(not_after=NOW)
    with pytest.raises(module.StorageInitializationError, match="manifest_expired"):
        module.parse_manifest(json.dumps(expired).encode(), now=NOW)
    recovered = module.parse_manifest(
        json.dumps(expired).encode(),
        now=NOW + timedelta(days=2),
        allow_expired_recovery=True,
    )
    assert recovered.not_after == NOW

    too_far = manifest_object(not_after=NOW + timedelta(hours=25))
    with pytest.raises(module.StorageInitializationError, match="expiry_too_far"):
        module.parse_manifest(json.dumps(too_far).encode(), now=NOW)

    for unsafe_change_id in ("A/../../etc", "CHG..20260825"):
        unsafe = manifest_object()
        unsafe["change_id"] = unsafe_change_id
        with pytest.raises(
            module.StorageInitializationError,
            match="manifest_change_id_invalid",
        ):
            module.parse_manifest(json.dumps(unsafe).encode(), now=NOW)


@pytest.mark.parametrize(
    "bad_uuid",
    (
        "not-a-uuid",
        "11111111-1111-1111-8111-111111111111",
        "11111111-1111-4111-7111-111111111111",
        "11111111-1111-4111-8111-11111111111A",
    ),
)
def test_manifest_rejects_noncanonical_or_non_v4_filesystem_uuid(bad_uuid: str) -> None:
    module = initializer_module()
    raw = manifest_object()
    cast(dict[str, dict[str, object]], raw["devices"])["docker"][
        "filesystem_uuid"
    ] = bad_uuid

    with pytest.raises(
        module.StorageInitializationError, match="manifest_filesystem_uuid_invalid"
    ):
        module.parse_manifest(json.dumps(raw).encode(), now=NOW)


def test_fstab_render_preserves_original_bytes_and_appends_one_exact_block() -> None:
    module = initializer_module()
    manifest = parsed_manifest(module)
    original = (
        b"# Ubuntu installer\n"
        b"UUID=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa / ext4 defaults 0 1\n"
    )

    rendered = module.render_fstab(original, manifest, "f" * 64)

    assert rendered.startswith(original)
    assert rendered.decode().splitlines()[-6:] == [
        "# BEGIN sms-platform production storage ffffffffffffffff",
        f"UUID={UUIDS['docker']} /var/lib/docker xfs defaults,nodev,nosuid 0 2",
        f"UUID={UUIDS['postgres']} /var/lib/sms-platform/postgres xfs defaults,nodev,nosuid 0 2",
        f"UUID={UUIDS['redis']} /var/lib/sms-platform/redis xfs defaults,nodev,nosuid 0 2",
        f"UUID={UUIDS['runtime']} /var/lib/sms-platform/runtime xfs defaults,nodev,nosuid 0 2",
        "# END sms-platform production storage",
    ]


def test_fstab_root_requires_a_canonical_uuid_source() -> None:
    module = initializer_module()
    original = b"UUID=------------------------------------ / ext4 defaults 0 1\n"

    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_root_contract_invalid",
    ):
        module.render_fstab(original, parsed_manifest(module), "f" * 64)


def test_fstab_root_rejects_read_only_or_wrong_fsck_order() -> None:
    module = initializer_module()
    original = (
        b"UUID=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa "
        b"/ ext4 defaults,ro 0 0\n"
    )

    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_root_contract_invalid",
    ):
        module.render_fstab(original, parsed_manifest(module), "f" * 64)


@pytest.mark.parametrize(
    ("suffix", "code"),
    (
        (
            "UUID=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb /var/lib/docker xfs defaults 0 2\n",
            "fstab_target_conflict",
        ),
        (
            "malformed /data\n",
            "fstab_malformed",
        ),
        (
            "/var/lib/docker/volumes /data none bind 0 0\n",
            "fstab_docker_internal_path",
        ),
        (
            "UUID=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb /var/lib/docker/ xfs defaults 0 2\n",
            "fstab_target_not_canonical",
        ),
    ),
)
def test_fstab_render_rejects_conflicts_and_malformed_input(
    suffix: str,
    code: str,
) -> None:
    module = initializer_module()
    original = (
        "UUID=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa / ext4 defaults 0 1\n"
        + suffix
    ).encode()

    with pytest.raises(module.StorageInitializationError, match=code):
        module.render_fstab(original, parsed_manifest(module), "e" * 64)


def test_fstab_target_path_rejects_a_symlink_component(tmp_path: Path) -> None:
    module = initializer_module()
    real_target = tmp_path / "real-target"
    alias_target = tmp_path / "alias-target"
    real_target.mkdir()
    alias_target.symlink_to(real_target, target_is_directory=True)
    payload = f"tmpfs {alias_target} tmpfs defaults 0 0\n".encode()

    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_target_(?:symlink|permissions_unsafe)",
    ):
        module._assert_fstab_target_paths_safe(payload)


def test_fstab_target_path_rejects_an_operator_writable_component(
    tmp_path: Path,
) -> None:
    module = initializer_module()
    target = tmp_path / "target"
    target.mkdir()
    payload = f"tmpfs {target} tmpfs defaults 0 0\n".encode()

    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_target_permissions_unsafe",
    ):
        module._assert_fstab_target_paths_safe(payload)


def test_chroot_root_identity_must_match_pid1_root(tmp_path: Path) -> None:
    module = initializer_module()
    host_root = tmp_path / "host-root"
    chroot_root = tmp_path / "chroot-root"
    host_root.mkdir()
    chroot_root.mkdir()

    module._assert_same_directory_object(
        host_root,
        host_root,
        code="chroot_execution_forbidden",
    )
    with pytest.raises(
        module.StorageInitializationError,
        match="chroot_execution_forbidden",
    ):
        module._assert_same_directory_object(
            chroot_root,
            host_root,
            code="chroot_execution_forbidden",
        )


def test_findmnt_verify_rejects_warnings_even_when_exit_code_is_zero() -> None:
    module = initializer_module()

    class WarningRunner:
        def run(  # type: ignore[no-untyped-def]
            self,
            argv,
            *,
            timeout_seconds: int,
            pass_fds=(),
        ):
            del argv, timeout_seconds, pass_fds
            return module.CommandResult(
                0,
                b"/mnt/alias\n [W] non-canonical target path\n",
                b"\n0 parse errors, 0 errors, 1 warning\n",
            )

    initializer = module.ProductionStorageInitializer(
        runner=WarningRunner(),
        effective_uid=0,
    )
    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_current_verification_failed",
    ):
        initializer._verify_fstab_file(
            Path("/etc/fstab"),
            code="fstab_current_verification_failed",
        )


def test_findmnt_verify_accepts_only_the_clean_success_summary() -> None:
    module = initializer_module()

    class CleanRunner:
        def run(  # type: ignore[no-untyped-def]
            self,
            argv,
            *,
            timeout_seconds: int,
            pass_fds=(),
        ):
            del argv, timeout_seconds, pass_fds
            return module.CommandResult(
                0,
                b"Success, no errors or warnings detected\n",
                b"",
            )

    initializer = module.ProductionStorageInitializer(
        runner=CleanRunner(),
        effective_uid=0,
    )
    initializer._verify_fstab_file(
        Path("/etc/fstab"),
        code="fstab_current_verification_failed",
    )


def lsblk_nodes(module: ModuleType):  # type: ignore[no-untyped-def]
    efi_partition = module.BlockNode(
        Path("/dev/sda1"),
        "part",
        512 * 1024**2,
        "",
        "",
        False,
        False,
        "8:1",
        "vfat",
        "",
        "abcd-1234",
        ("/boot/efi",),
        (),
        partition_type=module.EFI_SYSTEM_PARTITION_GUID,
    )
    root_partition = module.BlockNode(
        Path("/dev/sda2"),
        "part",
        99 * module.GIB,
        "",
        "",
        False,
        False,
        "8:2",
        "ext4",
        "",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ("/",),
        (),
        partition_type=module.LINUX_FILESYSTEM_PARTITION_GUID,
    )
    result = [
        module.BlockNode(
            Path("/dev/sda"),
            "disk",
            100 * module.GIB,
            "os-serial",
            "wwn-os",
            False,
            False,
            "8:0",
            "",
            "",
            "",
            (),
            (efi_partition, root_partition),
            partition_table_type="gpt",
        )
    ]
    for index, role in enumerate(("docker", "postgres", "redis", "runtime"), start=1):
        result.append(
            module.BlockNode(
                Path(f"/dev/sd{chr(ord('a') + index)}"),
                "disk",
                module.SPECS_BY_ROLE[role].nominal_bytes,
                f"{role}-serial",
                f"wwn-{role}",
                False,
                False,
                f"8:{index * 16}",
                "",
                "",
                "",
                (),
                (),
            )
        )
    return tuple(result)


class TopologyInitializer:  # wrapper avoids depending on a real block host
    def __init__(self, module: ModuleType) -> None:
        class Concrete(module.ProductionStorageInitializer):
            def _lsblk(inner_self):  # type: ignore[no-untyped-def]
                return lsblk_nodes(module)

            def _root_major_minor(inner_self) -> str:
                return "8:2"

            def _root_filesystem_capacity(inner_self) -> int:
                return 99 * module.GIB

            def _resolve_by_id(inner_self, path: Path):  # type: ignore[no-untyped-def]
                role = path.name.removeprefix("scsi-").removesuffix("-serial")
                index = ("os", "docker", "postgres", "redis", "runtime").index(role)
                return (
                    Path(f"/dev/sd{chr(ord('a') + index)}"),
                    SimpleNamespace(st_rdev=os.makedev(8, index * 16)),
                )

            def _assert_no_holders(inner_self, major_minor: str) -> None:
                del major_minor

            def _assert_blank_signatures(inner_self, path: Path) -> None:
                del path

            def _assert_zero_samples(inner_self, path: Path, size_bytes: int) -> None:
                del path, size_bytes

            def _block_device_size(inner_self, path: Path) -> int:
                role = path.name.removeprefix("sd")
                index = ord(role) - ord("a")
                return lsblk_nodes(module)[index].size_bytes

        self.instance = Concrete(effective_uid=0)


def test_device_topology_accepts_os_disk_with_partition_but_only_blank_whole_data_disks() -> None:
    module = initializer_module()
    initializer = TopologyInitializer(module).instance

    observations = initializer._observe_devices(  # type: ignore[attr-defined]
        parsed_manifest(module),
        require_blank=frozenset(module.DATA_ROLES),
    )

    assert observations["os"].major_minor == "8:0"
    assert {observations[role].major_minor for role in module.DATA_ROLES} == {
        "8:16",
        "8:32",
        "8:48",
        "8:64",
    }


def test_same_size_redis_alias_cannot_resolve_to_the_os_disk() -> None:
    module = initializer_module()
    initializer = TopologyInitializer(module).instance
    manifest = parsed_manifest(module)
    devices = dict(manifest.devices)
    devices["redis"] = replace(
        devices["redis"],
        by_id=Path("/dev/disk/by-id/scsi-os-serial"),
        expected_serial="os-serial",
    )
    unsafe = replace(manifest, devices=devices)

    with pytest.raises(
        module.StorageInitializationError,
        match="data_device_has_children|device_identity_reused|system_device_selected_as_data",
    ):
        initializer._observe_devices(  # type: ignore[attr-defined]
            unsafe,
            require_blank=frozenset(module.DATA_ROLES),
        )


def test_planned_data_uuid_cannot_reuse_an_existing_os_uuid() -> None:
    module = initializer_module()
    initializer = TopologyInitializer(module).instance
    nodes = list(lsblk_nodes(module))
    os_disk = nodes[0]
    root = replace(os_disk.children[1], filesystem_uuid=UUIDS["docker"])
    nodes[0] = replace(os_disk, children=(os_disk.children[0], root))
    initializer._lsblk = lambda: tuple(nodes)  # type: ignore[method-assign]

    with pytest.raises(
        module.StorageInitializationError,
        match="planned_filesystem_uuid_conflict",
    ):
        initializer._observe_devices(  # type: ignore[attr-defined]
            parsed_manifest(module),
            require_blank=frozenset(module.DATA_ROLES),
        )


def test_final_layout_rejects_a_second_mountpoint_for_any_data_disk() -> None:
    module = initializer_module()
    observations = {
        role: observation(module, role, index)
        for index, role in enumerate(("os", "docker", "postgres", "redis", "runtime"))
    }
    observations["docker"] = replace(
        observations["docker"],
        mountpoints=("/var/lib/docker", "/mnt/docker-alias"),
    )
    initializer = module.ProductionStorageInitializer(effective_uid=0)

    with pytest.raises(
        module.StorageInitializationError,
        match="unexpected_data_device_mountpoint",
    ):
        initializer._assert_exact_data_mountpoints(observations)


def test_completed_status_allows_only_fixed_runtime_docker_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = initializer_module()
    observations = {
        role: observation(module, role, index)
        for index, role in enumerate(("os", "docker", "postgres", "redis", "runtime"))
    }
    bind = module.DOCKER_BIND_MOUNT_SPECS[0]
    observations["postgres"] = replace(
        observations["postgres"],
        mountpoints=(
            "/var/lib/sms-platform/postgres",
            str(bind.target_path),
        ),
    )
    initializer = module.ProductionStorageInitializer(effective_uid=0)
    verified: list[tuple[object, object]] = []
    monkeypatch.setattr(
        initializer,
        "_verify_runtime_docker_bind_mount",
        lambda bind_spec, observed: verified.append((bind_spec, observed)),
    )

    initializer._assert_exact_data_mountpoints(
        observations,
        allow_runtime_docker_binds=True,
    )

    assert verified == [(bind, observations["postgres"])]
    observations["postgres"] = replace(
        observations["postgres"],
        mountpoints=(
            "/var/lib/sms-platform/postgres",
            "/var/lib/docker/volumes/not-approved/_data",
        ),
    )
    with pytest.raises(
        module.StorageInitializationError,
        match="unexpected_data_device_mountpoint",
    ):
        initializer._assert_exact_data_mountpoints(
            observations,
            allow_runtime_docker_binds=True,
        )


def test_runtime_docker_bind_readback_contract_is_exact(tmp_path: Path) -> None:
    module = initializer_module()
    target = tmp_path / "bind-target"
    target.mkdir()
    metadata = target.stat()
    major_minor = f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
    observed = replace(
        observation(module, "postgres", 2),
        major_minor=major_minor,
    )
    bind = module.DockerBindMountSpec(
        "postgres",
        Path("/var/lib/sms-platform/postgres/pgdata"),
        target,
    )

    class BindReadbackInitializer(module.ProductionStorageInitializer):
        fsroot = "/pgdata"

        def _findmnt_record(self, requested: Path):  # type: ignore[no-untyped-def]
            assert requested == target
            return {
                "fsroot": self.fsroot,
                "fstype": "xfs",
                "maj:min": major_minor,
                "options": "rw,nodev,nosuid",
                "target": str(target),
            }

    initializer = BindReadbackInitializer(effective_uid=0)
    initializer._verify_runtime_docker_bind_mount(bind, observed)
    initializer.fsroot = "/wrong"

    with pytest.raises(
        module.StorageInitializationError,
        match="docker_bind_mount_contract_mismatch",
    ):
        initializer._verify_runtime_docker_bind_mount(bind, observed)


def test_mount_readback_rejects_filesystem_larger_than_its_block_device() -> None:
    module = initializer_module()
    observed = observation(module, "postgres", 2)

    class OversizedFilesystemInitializer(module.ProductionStorageInitializer):
        def _findmnt_record(self, target: Path):  # type: ignore[no-untyped-def]
            return {
                "fsroot": "/",
                "fstype": "xfs",
                "maj:min": observed.major_minor,
                "options": "rw,nodev,nosuid",
                "size": observed.size_bytes + 1,
                "target": str(target),
            }

    initializer = OversizedFilesystemInitializer(effective_uid=0)
    with pytest.raises(
        module.StorageInitializationError,
        match="mounted_filesystem_contract_mismatch",
    ):
        initializer._verify_mount("postgres", observed)


def test_completed_state_allows_growth_but_never_shrink() -> None:
    module = initializer_module()
    observations = {
        role: observation(module, role, index)
        for index, role in enumerate(("os", "docker", "postgres", "redis", "runtime"))
    }
    plan = {
        "devices": [observations[spec.role].plan_payload() for spec in module.ROLE_SPECS]
    }
    observations["postgres"] = replace(
        observations["postgres"],
        size_bytes=observations["postgres"].size_bytes + 100 * module.GIB,
    )
    initializer = module.ProductionStorageInitializer(effective_uid=0)

    initializer._assert_stable_plan_devices(
        plan,
        observations,
        allow_device_growth=True,
    )
    with pytest.raises(
        module.StorageInitializationError,
        match="planned_device_identity_changed",
    ):
        initializer._assert_stable_plan_devices(plan, observations)

    observations["postgres"] = replace(
        observations["postgres"],
        size_bytes=module.SPECS_BY_ROLE["postgres"].nominal_bytes - 1,
    )
    with pytest.raises(
        module.StorageInitializationError,
        match="planned_device_identity_changed",
    ):
        initializer._assert_stable_plan_devices(
            plan,
            observations,
            allow_device_growth=True,
        )


def test_resume_requires_same_script_machine_and_vm_but_allows_new_boot_id() -> None:
    module = initializer_module()

    class ContextInitializer(module.ProductionStorageInitializer):
        def _script_sha256(self) -> str:
            return "a" * 64

        def _host_identity(self):  # type: ignore[no-untyped-def]
            return {
                "boot_id": "22222222-2222-4222-8222-222222222222",
                "machine_id_sha256": "b" * 64,
                "product_uuid_sha256": "c" * 64,
            }

    initializer = ContextInitializer(effective_uid=0)
    plan = {
        "host_identity": {
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "machine_id_sha256": "b" * 64,
            "product_uuid_sha256": "c" * 64,
        },
        "script_sha256": "a" * 64,
    }

    initializer._assert_recovery_context(plan)
    plan["script_sha256"] = "d" * 64
    with pytest.raises(
        module.StorageInitializationError,
        match="recovery_context_invalid",
    ):
        initializer._assert_recovery_context(plan)

    plan["script_sha256"] = "a" * 64
    cast(dict[str, str], plan["host_identity"])["machine_id_sha256"] = "e" * 64
    with pytest.raises(
        module.StorageInitializationError,
        match="recovery_host_changed",
    ):
        initializer._assert_recovery_context(plan)


def test_fstab_block_alias_cannot_reference_a_selected_data_disk() -> None:
    module = initializer_module()
    observations = {
        role: observation(module, role, index)
        for index, role in enumerate(("os", "docker", "postgres", "redis", "runtime"))
    }

    class AliasInitializer(module.ProductionStorageInitializer):
        def _fstab_source_major_minor(self, source: str, fs_type: str):  # type: ignore[no-untyped-def]
            del source, fs_type
            return observations["docker"].major_minor

    initializer = AliasInitializer(effective_uid=0)
    with pytest.raises(
        module.StorageInitializationError,
        match="fstab_references_data_device",
    ):
        initializer._assert_fstab_sources_disjoint_from_data(
            b"/dev/disk/by-id/scsi-docker-serial /mnt/old xfs defaults 0 2\n",
            observations,
        )


class RecordingRunner:
    def __init__(self, module: ModuleType) -> None:
        self.module = module
        self.calls: list[tuple[str, ...]] = []
        self.inherited_fds: list[tuple[int, ...]] = []

    def run(  # type: ignore[no-untyped-def]
        self,
        argv,
        *,
        timeout_seconds: int,
        pass_fds=(),
    ):
        del timeout_seconds
        self.calls.append(tuple(argv))
        self.inherited_fds.append(tuple(pass_fds))
        if argv[0] == self.module.XFS_INFO_BINARY:
            return self.module.CommandResult(
                0,
                b"naming   =version 2              bsize=4096   ascii-ci=0, ftype=1\n",
            )
        return self.module.CommandResult(0)


def test_all_four_xfs_filesystems_require_ftype_one() -> None:
    module = initializer_module()

    class XfsInfoRunner:
        def __init__(self, bad_target: str | None = None) -> None:
            self.bad_target = bad_target
            self.targets: list[str] = []

        def run(  # type: ignore[no-untyped-def]
            self,
            argv,
            *,
            timeout_seconds: int,
            pass_fds=(),
        ):
            del timeout_seconds, pass_fds
            assert argv[0] == module.XFS_INFO_BINARY
            target = argv[1]
            self.targets.append(target)
            ftype = b"0" if target == self.bad_target else b"1"
            return module.CommandResult(
                0,
                b"naming   =version 2              bsize=4096   ascii-ci=0, ftype="
                + ftype
                + b"\n",
            )

    clean_runner = XfsInfoRunner()
    initializer = module.ProductionStorageInitializer(
        runner=clean_runner,
        effective_uid=0,
    )
    initializer._verify_all_xfs_ftype()
    assert clean_runner.targets == [str(spec.mount_path) for spec in module.DATA_SPECS]

    postgres_target = str(module.SPECS_BY_ROLE["postgres"].mount_path)
    unsafe = module.ProductionStorageInitializer(
        runner=XfsInfoRunner(postgres_target),
        effective_uid=0,
    )
    with pytest.raises(module.StorageInitializationError, match="xfs_ftype_required"):
        unsafe._verify_all_xfs_ftype()


def test_existing_fstab_artifacts_are_fsynced_before_recovery_early_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = initializer_module()
    expected = b"tmpfs / tmpfs defaults 0 0\n"
    synchronized: list[tuple[Path, bytes, str]] = []

    def sync_existing(path: Path, **kwargs):  # type: ignore[no-untyped-def]
        synchronized.append(
            (path, kwargs["expected_payload"], kwargs["mismatch_code"])
        )

    monkeypatch.setattr(module, "_fsync_existing_regular_secure", sync_existing)
    initializer = module.ProductionStorageInitializer(effective_uid=0)
    backup_path = tmp_path / "fstab.test.bak"
    backup_path.write_bytes(expected)
    initializer._secure_backup(backup_path, expected)

    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_ino=42)
    monkeypatch.setattr(
        module,
        "_read_regular_secure",
        lambda *args, **kwargs: (expected, metadata),
    )
    initializer._replace_fstab(
        original=b"old",
        expected=expected,
        original_inode=1,
    )

    assert synchronized == [
        (backup_path, expected, "fstab_backup_mismatch"),
        (Path("/etc/fstab"), expected, "fstab_changed_since_plan"),
    ]


def test_atomic_create_uses_one_name_publication_and_leaves_no_hidden_hardlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = initializer_module()
    destination = tmp_path / "state.json"
    synced: list[Path] = []

    def publish(source: Path, target: Path) -> None:
        assert source.parent == target.parent == tmp_path
        os.rename(source, target)

    monkeypatch.setattr(module, "_rename_noreplace", publish)
    monkeypatch.setattr(module, "_fsync_directory", synced.append)
    monkeypatch.setattr(module.os, "fchown", lambda *args: None)

    module._atomic_create(
        destination,
        b"sealed\n",
        mode=0o600,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert destination.read_bytes() == b"sealed\n"
    assert destination.stat().st_nlink == 1
    assert list(tmp_path.iterdir()) == [destination]
    assert synced == [tmp_path]


def test_existing_fixed_directory_is_fsynced_with_its_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = initializer_module()
    directory = tmp_path / "fixed"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    synchronized: list[Path] = []
    monkeypatch.setattr(module, "_fsync_directory", synchronized.append)

    module._create_fixed_directory(
        directory,
        uid=os.getuid(),
        gid=os.getgid(),
        mode=0o700,
    )

    assert synchronized == [directory, tmp_path]


def test_mkfs_command_has_fixed_uuid_ftype_and_no_force_or_partition_tool() -> None:
    module = initializer_module()
    runner = RecordingRunner(module)

    class FormatInitializer(module.ProductionStorageInitializer):
        @contextmanager
        def _open_revalidated_blank_device(self, observed):  # type: ignore[no-untyped-def]
            assert observed.role == "docker"
            yield 73

        def _verify_expected_filesystem(  # type: ignore[no-untyped-def]
            self,
            role: str,
            path: Path,
            *,
            pass_fds=(),
        ) -> None:
            assert role == "docker"
            if path == Path("/proc/self/fd/73"):
                assert pass_fds == (73, 91)
            else:
                assert path == Path("/dev/disk/by-id/scsi-docker-serial")
                assert pass_fds == ()

    initializer = FormatInitializer(runner=runner, effective_uid=0)
    initializer._active_manifest = parsed_manifest(module)
    initializer._active_lock_fd = 91
    initializer._format_role("docker", observation(module, "docker", 1))

    assert runner.calls == [
        (module.UDEVADM_BINARY, "settle", "--timeout=30"),
        (
            module.MKFS_XFS_BINARY,
            "-q",
            "-L",
            "sms_docker",
            "-m",
            f"uuid={UUIDS['docker']}",
            "-n",
            "ftype=1",
            "/proc/self/fd/73",
        ),
        (module.XFS_INFO_BINARY, "/proc/self/fd/73"),
        (module.UDEVADM_BINARY, "settle", "--timeout=30"),
    ]
    assert runner.inherited_fds == [(), (73, 91), (73, 91), ()]
    serialized = " ".join(runner.calls[1])
    assert " -f " not in f" {serialized} "
    assert all(tool not in serialized for tool in ("wipefs", "parted", "fdisk", "pvcreate"))


class SimulationInitializer:  # exercises the durable state machine without block devices
    def __init__(self, module: ModuleType) -> None:
        class Concrete(module.ProductionStorageInitializer):
            def _write_intent(inner_self, intent):  # type: ignore[no-untyped-def]
                inner_self.persisted_intent = copy.deepcopy(intent)

            def _phase_observations(
                inner_self,
                intent,
                *,
                current_role_may_be_complete: bool,
            ):  # type: ignore[no-untyped-def]
                del current_role_may_be_complete
                current = intent["current_role"]
                return inner_self.observations, current in inner_self.formatted_devices

            def _format_role(inner_self, role: str, observed):  # type: ignore[no-untyped-def]
                del observed
                inner_self.mkfs_counts[role] = inner_self.mkfs_counts.get(role, 0) + 1
                inner_self.formatted_devices.add(role)
                if inner_self.fail_after_format == role:
                    inner_self.fail_after_format = None
                    raise module.StorageInitializationError("injected_after_mkfs")

            def _prepare_fstab_intent(inner_self, intent):  # type: ignore[no-untyped-def]
                updated = {
                    **intent,
                    "fstab": {
                        "backup_path": "/etc/fstab.sms-platform.test.bak",
                        "expected_sha256": "2" * 64,
                        "original_inode": 1,
                        "original_sha256": "1" * 64,
                    },
                    "phase": "fstab_prepared",
                }
                inner_self._write_intent(updated)
                return updated, b"old", b"new"

            def _secure_backup(inner_self, path: Path, expected_payload: bytes) -> None:
                assert path == Path("/etc/fstab.sms-platform.test.bak")
                assert expected_payload == b"old"

            def _replace_fstab(
                inner_self,
                *,
                original: bytes,
                expected: bytes,
                original_inode: int,
            ) -> None:
                assert (original, expected, original_inode) == (b"old", b"new", 1)

            def _observe_devices(inner_self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return inner_self.observations

            def _assert_stable_plan_devices(inner_self, plan, observations):  # type: ignore[no-untyped-def]
                del plan, observations

            def _inspect_mountpoint_candidates(inner_self) -> None:
                return None

            def _ensure_mountpoints(inner_self, observations):  # type: ignore[no-untyped-def]
                del observations

            def _mount_role(inner_self, role: str, observed):  # type: ignore[no-untyped-def]
                del observed
                inner_self.mounted_devices.add(role)

            def _set_mount_permissions_and_directories(inner_self, observations):  # type: ignore[no-untyped-def]
                del observations
                inner_self.directories_done = True

            def _verify_final_layout(inner_self, plan):  # type: ignore[no-untyped-def]
                del plan
                assert inner_self.formatted_devices == set(module.DATA_ROLES)
                assert inner_self.mounted_devices == set(module.DATA_ROLES)
                assert inner_self.directories_done is True
                if inner_self.fail_final_once:
                    inner_self.fail_final_once = False
                    raise module.StorageInitializationError("injected_final_failure")
                return inner_self.observations

            def _verify_xfs_ftype(inner_self, path: Path, *, pass_fds=()) -> None:
                del pass_fds
                inner_self.ftype_checked_paths.append(path)

            def _write_state(inner_self, state):  # type: ignore[no-untyped-def]
                inner_self.state = copy.deepcopy(state)

            def _clear_intent(inner_self) -> None:
                inner_self.persisted_intent = None

        self.instance = Concrete(effective_uid=0)
        self.instance._active_manifest = parsed_manifest(module)
        self.instance.observations = {
            role: observation(module, role, index)
            for index, role in enumerate(
                ("os", "docker", "postgres", "redis", "runtime")
            )
        }
        self.instance.persisted_intent = None
        self.instance.formatted_devices: set[str] = set()
        self.instance.mounted_devices: set[str] = set()
        self.instance.mkfs_counts: dict[str, int] = {}
        self.instance.fail_after_format: str | None = None
        self.instance.directories_done = False
        self.instance.fail_final_once = False
        self.instance.state = None
        self.instance.ftype_checked_paths: list[Path] = []


def simulated_plan(module: ModuleType):  # type: ignore[no-untyped-def]
    canonical = {
        "change_id": "CHG-20260825-001",
        "devices": [],
        "plan_schema_version": 1,
    }
    return module.LivePlan(
        canonical=canonical,
        sha256=module._sha256(module._canonical_json(canonical)),
        observations={},
        confirmation_token="unused",
    )


def test_interruption_after_mkfs_resumes_without_formatting_that_disk_twice() -> None:
    module = initializer_module()
    simulation = SimulationInitializer(module).instance
    intent = simulation._new_intent(simulated_plan(module))
    simulation.fail_after_format = "docker"

    with pytest.raises(module.StorageInitializationError, match="injected_after_mkfs"):
        simulation._continue(intent)

    interrupted = copy.deepcopy(simulation.persisted_intent)
    assert interrupted["phase"] == "formatting"
    assert interrupted["current_role"] == "docker"
    assert simulation.mkfs_counts == {"docker": 1}

    result = simulation._continue(interrupted)

    assert result["status"] == "initialized"
    assert simulation.mkfs_counts == {
        "docker": 1,
        "postgres": 1,
        "redis": 1,
        "runtime": 1,
    }
    assert simulation.persisted_intent is None
    assert simulation.state["status"] == "initialized"
    assert simulation.ftype_checked_paths[-4:] == [
        simulation.observations[role].by_id for role in module.DATA_ROLES
    ]


def test_resume_rechecks_all_mounts_after_a_reboot_even_if_checkpoint_says_mounted() -> None:
    module = initializer_module()
    simulation = SimulationInitializer(module).instance
    intent = simulation._new_intent(simulated_plan(module))
    simulation.fail_final_once = True

    with pytest.raises(module.StorageInitializationError, match="injected_final_failure"):
        simulation._continue(intent)

    interrupted = copy.deepcopy(simulation.persisted_intent)
    assert interrupted["mounted"] == list(module.DATA_ROLES)
    interrupted["fstab"] = None
    simulation.mounted_devices.clear()

    result = simulation._continue(interrupted)

    assert result["status"] == "initialized"
    assert simulation.mounted_devices == set(module.DATA_ROLES)


class RejectConfirmation:
    def __init__(self, module: ModuleType) -> None:
        self.module = module
        self.called = False

    def confirm(self, plan) -> None:  # type: ignore[no-untyped-def]
        del plan
        self.called = True
        raise self.module.StorageInitializationError("interactive_confirmation_failed")


def test_apply_confirmation_failure_occurs_before_control_or_destructive_write() -> None:
    module = initializer_module()
    confirmation = RejectConfirmation(module)
    plan = simulated_plan(module)

    class GateInitializer(module.ProductionStorageInitializer):
        def _read_manifest(self):  # type: ignore[no-untyped-def]
            return parsed_manifest(module)

        def _build_plan(self, manifest):  # type: ignore[no-untyped-def]
            del manifest
            return plan

        def _ensure_control_directory(self) -> None:
            raise AssertionError("must not write before confirmation")

    initializer = GateInitializer(
        confirmation_reader=confirmation,
        effective_uid=0,
    )

    with pytest.raises(module.StorageInitializationError, match="interactive_confirmation_failed"):
        initializer.apply(plan.sha256)

    assert confirmation.called is True


def test_existing_0755_sms_parent_is_blocked_before_any_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = initializer_module()

    class ExistingParent:
        @staticmethod
        def lstat():  # type: ignore[no-untyped-def]
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)

    monkeypatch.setattr(module, "SMS_STORAGE_ROOT", ExistingParent())
    monkeypatch.setattr(module, "DATA_SPECS", ())
    monkeypatch.setattr(module, "_assert_path_chain", lambda path, *, allow_missing: True)
    initializer = module.ProductionStorageInitializer(effective_uid=0)

    with pytest.raises(
        module.StorageInitializationError,
        match="storage_parent_contract_mismatch",
    ):
        initializer._assert_storage_parent_contract(allow_missing=False)


def test_already_initialized_requires_the_exact_supplied_plan_sha(
    tmp_path: Path,
) -> None:
    module = initializer_module()
    exact = "a" * 64
    state_path = tmp_path / "state.json"
    state_path.touch()

    class ExistingInitializer(module.ProductionStorageInitializer):
        def status(self):  # type: ignore[no-untyped-def]
            return {
                "action": "status",
                "plan_sha256": exact,
                "status": "initialized",
            }

    initializer = ExistingInitializer(
        state_path=state_path,
        intent_path=tmp_path / "intent.json",
        manifest_path=tmp_path / "manifest.json",
        lock_path=tmp_path / "lock",
        effective_uid=0,
    )

    for action in (initializer.apply, initializer.resume):
        with pytest.raises(
            module.StorageInitializationError,
            match="plan_sha256_mismatch",
        ):
            action("b" * 64)
        assert action(exact)["status"] == "already_initialized"


def test_plan_rejects_manifest_outside_the_control_directory(tmp_path: Path) -> None:
    module = initializer_module()
    initializer = module.ProductionStorageInitializer(
        manifest_path=tmp_path / "elsewhere/manifest.json",
        state_path=tmp_path / "control/state.json",
        intent_path=tmp_path / "control/intent.json",
        lock_path=tmp_path / "control/lock",
        effective_uid=0,
    )

    with pytest.raises(
        module.StorageInitializationError,
        match="control_paths_must_share_parent",
    ):
        initializer._require_control_path_contract()


def test_cli_has_no_force_wipe_generic_device_or_noninteractive_bypass() -> None:
    module = initializer_module()
    parser = module._parser()
    help_text = parser.format_help()

    assert "--device" not in help_text
    assert "--force" not in help_text
    assert "--yes" not in help_text
    assert "--non-interactive" not in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["apply", "--device", "/dev/sdb"])


def test_cli_failure_emits_only_bounded_safe_code(monkeypatch: pytest.MonkeyPatch) -> None:
    module = initializer_module()
    secret = "super-secret-password-10.23.45.67"

    def blocked(self):  # type: ignore[no-untyped-def]
        raise module.StorageInitializationError("manifest_json_invalid") from RuntimeError(
            secret
        )

    monkeypatch.setattr(module.ProductionStorageInitializer, "plan", blocked)
    output = io.StringIO()
    error = io.StringIO()

    assert module.main(["plan"], stdout=output, stderr=error) == 1
    assert output.getvalue() == ""
    assert secret not in error.getvalue()
    assert json.loads(error.getvalue()) == {
        "action": "plan",
        "safe_code": "manifest_json_invalid",
        "status": "blocked",
    }


def test_subprocess_runner_uses_fixed_minimal_environment_and_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = initializer_module()
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setenv("INHERITED_SECRET", "must-not-cross-boundary")

    result = module.SubprocessRunner().run(
        (module.LSBLK_BINARY, "--version"), timeout_seconds=7
    )

    assert result == module.CommandResult(0, b"ok", b"")
    assert observed["shell"] is False
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["timeout"] == 7
    assert observed["pass_fds"] == ()
    assert observed["env"] == {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONNOUSERSITE": "1",
    }


def test_plan_and_absent_status_do_not_create_control_or_lock_files(tmp_path: Path) -> None:
    module = initializer_module()
    plan = simulated_plan(module)
    control = tmp_path / "etc/sms-platform"
    control.mkdir(parents=True)

    class ReadOnlyInitializer(module.ProductionStorageInitializer):
        def _read_manifest(self):  # type: ignore[no-untyped-def]
            return parsed_manifest(module)

        def _build_plan(self, manifest):  # type: ignore[no-untyped-def]
            del manifest
            return plan

    initializer = ReadOnlyInitializer(
        manifest_path=control / "manifest.json",
        state_path=control / "state.json",
        intent_path=control / "intent.json",
        lock_path=control / "lock",
    )
    before = list(tmp_path.rglob("*"))

    assert initializer.plan()["plan_sha256"] == plan.sha256
    assert initializer.status() == {"action": "status", "status": "absent"}

    assert list(tmp_path.rglob("*")) == before


def test_completed_status_enables_growth_and_exact_runtime_bind_readback(
    tmp_path: Path,
) -> None:
    module = initializer_module()
    state_path = tmp_path / "state.json"
    state_path.write_text("{}\n", encoding="utf-8")
    observed_options: dict[str, bool] = {}
    plan = {"change_id": "CHG-20260825-001"}
    state = {"plan_sha256": "1" * 64}

    class CompletedStatusInitializer(module.ProductionStorageInitializer):
        def _require_tools(self) -> None:
            return None

        def _require_real_vm_host(self) -> None:
            return None

        def _require_root_filesystem_contract(self) -> None:
            return None

        def _read_control(self, path: Path):  # type: ignore[no-untyped-def]
            assert path == state_path
            return state

        def _validate_state(self, value):  # type: ignore[no-untyped-def]
            assert value == state
            return plan

        def _assert_recovery_context(self, value):  # type: ignore[no-untyped-def]
            assert value == plan

        def _verify_final_layout(self, value, **kwargs):  # type: ignore[no-untyped-def]
            assert value == plan
            observed_options.update(kwargs)
            return {}

    initializer = CompletedStatusInitializer(
        state_path=state_path,
        intent_path=tmp_path / "missing-intent.json",
        effective_uid=0,
    )

    assert initializer.status()["status"] == "initialized"
    assert observed_options == {
        "allow_device_growth": True,
        "allow_runtime_docker_binds": True,
    }


def test_initializer_is_not_referenced_by_application_release_or_update_entrypoints() -> None:
    for path in (
        ROOT / "deploy/sms-compose",
        ROOT / "deploy/scripts/install_production_host_assets.py",
        ROOT / "scripts/test_update.sh",
    ):
        assert SCRIPT.name not in path.read_text(encoding="utf-8")


def test_source_uses_no_shell_and_contains_no_signature_erasure_or_false_rollback() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert "shell=False" in source
    assert 'WIPEFS_BINARY, "--no-act", "--json"' in source
    assert "MKFS_XFS_BINARY" in source
    assert 'Path(f"/proc/self/fd/{descriptor}")' in source
    for forbidden in (
        "wipefs -a",
        "wipefs --all",
        "sgdisk",
        "parted",
        "pvcreate",
        "vgcreate",
        "lvcreate",
        "mount -a",
        "rollback(",
        "subprocess.Popen",
        "shell=True",
    ):
        assert forbidden not in source
