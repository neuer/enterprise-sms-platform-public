from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import production_host_preflight as preflight  # noqa: E402
from production_host_preflight import (  # noqa: E402
    BASE_CHECK_NAMES,
    RUNTIME_CHECK_NAMES,
    CommandResult,
    inspect_host,
)

SCRIPT = SCRIPTS / "production_host_preflight.py"


class FakeProbe:
    def __init__(self) -> None:
        self.os_release = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        self.architecture = "x86_64"
        self.kernel = "6.8.0-83-generic"
        self.vcpu_count = 12
        self.meminfo = "MemTotal:       49807360 kB\n"
        self.systemd_runtime = True
        self.calls: list[tuple[str, ...]] = []
        self.results: dict[tuple[str, ...], CommandResult] = {
            (preflight.PYTHON_BINARY, "--version"): CommandResult(0, "Python 3.12.11\n"),
            (preflight.SYSTEMCTL_BINARY, "--version"): CommandResult(
                0, "systemd 255 (255.4-1ubuntu8.11)\n"
            ),
            (
                preflight.TIMEDATECTL_BINARY,
                "show",
                "--property=NTPSynchronized",
                "--value",
            ): CommandResult(0, "yes\n"),
            (
                preflight.TIMEDATECTL_BINARY,
                "show",
                "--property=Timezone",
                "--value",
            ): CommandResult(0, "Asia/Shanghai\n"),
            (preflight.VMWARE_TOOLBOX_BINARY, "-v"): CommandResult(
                0, "12.5.2.24964812 (build-24964812)\n"
            ),
            preflight.OPEN_VM_TOOLS_SERVICE_COMMAND: CommandResult(0),
            (preflight.DOCKER_BINARY, "--version"): CommandResult(
                0, "Docker version 28.3.3, build 980b856\n"
            ),
            (
                preflight.DOCKER_BINARY,
                "compose",
                "version",
                "--short",
            ): CommandResult(0, "2.39.1\n"),
            preflight.DOCKER_ROOT_INFO_COMMAND: CommandResult(
                0, json.dumps(preflight.DOCKER_ROOT_PATH) + "\n"
            ),
            preflight.DOCKER_DRIVER_INFO_COMMAND: CommandResult(0, json.dumps("overlay2") + "\n"),
            preflight.DOCKER_DRIVER_STATUS_COMMAND: CommandResult(
                0,
                json.dumps(
                    [
                        ["Backing Filesystem", "extfs"],
                        ["Supports d_type", "true"],
                    ]
                )
                + "\n",
            ),
            preflight.DOCKER_LOGGING_DRIVER_COMMAND: CommandResult(
                0, json.dumps("json-file") + "\n"
            ),
            preflight.DOCKER_CGROUP_VERSION_COMMAND: CommandResult(0, json.dumps("2") + "\n"),
            preflight.DOCKER_CGROUP_DRIVER_COMMAND: CommandResult(0, json.dumps("systemd") + "\n"),
            (preflight.OPENSSL_BINARY, "version"): CommandResult(0, "OpenSSL 3.0.13 30 Jan 2024\n"),
            (preflight.RSYNC_BINARY, "--version"): CommandResult(
                0, "rsync  version 3.2.7  protocol version 31\n"
            ),
            (preflight.SSH_BINARY, "-V"): CommandResult(
                0, stderr="OpenSSH_9.6p1 Ubuntu-3ubuntu13.14\n"
            ),
            (preflight.PSQL_BINARY, "--version"): CommandResult(
                0, "psql (PostgreSQL) 16.10 (Ubuntu 16.10-1.pgdg24.04+1)\n"
            ),
            (preflight.FINDMNT_BINARY, "--version"): CommandResult(
                0, "findmnt from util-linux 2.39.3\n"
            ),
            (preflight.LSBLK_BINARY, "--version"): CommandResult(
                0, "lsblk from util-linux 2.39.3\n"
            ),
            (preflight.BLKID_BINARY, "--version"): CommandResult(
                0, "blkid from util-linux 2.39.3 (libblkid 2.39.3, 04-Aug-2023)\n"
            ),
            (preflight.XFS_INFO_BINARY, "-V"): CommandResult(0, "xfs_info version 6.8.0\n"),
            preflight.DOCKER_FILESYSTEM_COMMAND: CommandResult(0, "ext4\n"),
        }

    def read_text(self, path: Path, maximum_bytes: int) -> str:
        if path == preflight.OS_RELEASE_PATH:
            assert maximum_bytes == preflight.MAX_OS_RELEASE_BYTES
            return self.os_release
        if path == preflight.MEMINFO_PATH:
            assert maximum_bytes == preflight.MAX_MEMINFO_BYTES
            return self.meminfo
        raise AssertionError(f"unexpected read path: {path}")

    def machine(self) -> str:
        return self.architecture

    def kernel_release(self) -> str:
        return self.kernel

    def cpu_count(self) -> int | None:
        return self.vcpu_count

    def is_directory(self, path: Path) -> bool:
        assert path == preflight.SYSTEMD_RUNTIME_PATH
        return self.systemd_runtime

    def run(self, argv: Sequence[str]) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        return self.results[key]


def result_by_name(probe: FakeProbe, name: str) -> preflight.CheckResult:
    profile: preflight.HostProfile = (
        "runtime"
        if name
        in {
            "docker_root_dir",
            "docker_storage_backend",
            "docker_logging_driver",
            "docker_cgroup_version",
            "docker_cgroup_driver",
        }
        else "base"
    )
    return next(
        check for check in inspect_host(probe, profile=profile).checks if check.name == name
    )


def test_complete_ubuntu_2404_amd64_baseline_passes() -> None:
    probe = FakeProbe()

    report = inspect_host(probe)

    assert report.passed is True
    assert report.profile == "base"
    assert tuple(check.name for check in report.checks) == BASE_CHECK_NAMES
    assert all(check.passed for check in report.checks)
    assert result_by_name(probe, "architecture").version == "amd64"
    assert result_by_name(probe, "host_python").version == "3.12.11"
    assert result_by_name(probe, "docker_compose").version == "2.39.1"
    assert result_by_name(probe, "psql").version == "16.10"


def test_probe_runs_only_fixed_read_only_version_and_status_commands() -> None:
    probe = FakeProbe()

    inspect_host(probe)

    assert probe.calls == [
        (preflight.PYTHON_BINARY, "--version"),
        (preflight.SYSTEMCTL_BINARY, "--version"),
        (
            preflight.TIMEDATECTL_BINARY,
            "show",
            "--property=NTPSynchronized",
            "--value",
        ),
        (
            preflight.TIMEDATECTL_BINARY,
            "show",
            "--property=Timezone",
            "--value",
        ),
        (preflight.VMWARE_TOOLBOX_BINARY, "-v"),
        preflight.OPEN_VM_TOOLS_SERVICE_COMMAND,
        (preflight.DOCKER_BINARY, "--version"),
        (preflight.DOCKER_BINARY, "compose", "version", "--short"),
        (preflight.OPENSSL_BINARY, "version"),
        (preflight.RSYNC_BINARY, "--version"),
        (preflight.SSH_BINARY, "-V"),
        (preflight.PSQL_BINARY, "--version"),
        (preflight.FINDMNT_BINARY, "--version"),
        (preflight.LSBLK_BINARY, "--version"),
        (preflight.BLKID_BINARY, "--version"),
        (preflight.XFS_INFO_BINARY, "-V"),
        preflight.DOCKER_FILESYSTEM_COMMAND,
    ]


def test_base_profile_does_not_require_a_running_docker_daemon() -> None:
    probe = FakeProbe()
    daemon_commands = (
        preflight.DOCKER_ROOT_INFO_COMMAND,
        preflight.DOCKER_DRIVER_INFO_COMMAND,
        preflight.DOCKER_DRIVER_STATUS_COMMAND,
        preflight.DOCKER_LOGGING_DRIVER_COMMAND,
        preflight.DOCKER_CGROUP_VERSION_COMMAND,
        preflight.DOCKER_CGROUP_DRIVER_COMMAND,
    )
    for command in daemon_commands:
        probe.results[command] = CommandResult(1)

    report = inspect_host(probe)

    assert report.passed is True
    assert all(command not in probe.calls for command in daemon_commands)


def test_runtime_profile_rechecks_base_then_reads_docker_daemon_state() -> None:
    probe = FakeProbe()

    report = inspect_host(probe, profile="runtime")

    assert report.passed is True
    assert report.profile == "runtime"
    assert tuple(check.name for check in report.checks) == RUNTIME_CHECK_NAMES
    assert probe.calls[-6:] == [
        preflight.DOCKER_ROOT_INFO_COMMAND,
        preflight.DOCKER_DRIVER_INFO_COMMAND,
        preflight.DOCKER_DRIVER_STATUS_COMMAND,
        preflight.DOCKER_LOGGING_DRIVER_COMMAND,
        preflight.DOCKER_CGROUP_VERSION_COMMAND,
        preflight.DOCKER_CGROUP_DRIVER_COMMAND,
    ]


def test_systemd_major_version_is_pinned_to_255() -> None:
    probe = FakeProbe()
    probe.results[(preflight.SYSTEMCTL_BINARY, "--version")] = CommandResult(
        0, "systemd 256 (256.9-1ubuntu1)\n"
    )

    check = result_by_name(probe, "systemd")

    assert check.payload() == {
        "passed": False,
        "reason": "systemd_255_required",
        "version": "256",
    }


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "operating_system",
            lambda probe: setattr(probe, "os_release", 'ID=ubuntu\nVERSION_ID="22.04"\n'),
        ),
        ("architecture", lambda probe: setattr(probe, "architecture", "aarch64")),
        ("ga_kernel", lambda probe: setattr(probe, "kernel", "6.14.0-29-generic")),
        ("guest_vcpu", lambda probe: setattr(probe, "vcpu_count", 8)),
        (
            "guest_memory",
            lambda probe: setattr(
                probe,
                "meminfo",
                f"MemTotal: {preflight.MINIMUM_MEMORY_KIB - 1} kB\n",
            ),
        ),
        (
            "host_python",
            lambda probe: probe.results.__setitem__(
                (preflight.PYTHON_BINARY, "--version"),
                CommandResult(0, "Python 3.11.9\n"),
            ),
        ),
        ("systemd", lambda probe: setattr(probe, "systemd_runtime", False)),
        (
            "time_synchronized",
            lambda probe: probe.results.__setitem__(
                (
                    preflight.TIMEDATECTL_BINARY,
                    "show",
                    "--property=NTPSynchronized",
                    "--value",
                ),
                CommandResult(0, "no\n"),
            ),
        ),
        (
            "host_timezone",
            lambda probe: probe.results.__setitem__(
                (
                    preflight.TIMEDATECTL_BINARY,
                    "show",
                    "--property=Timezone",
                    "--value",
                ),
                CommandResult(0, "UTC\n"),
            ),
        ),
        (
            "open_vm_tools",
            lambda probe: probe.results.__setitem__(
                preflight.OPEN_VM_TOOLS_SERVICE_COMMAND,
                CommandResult(3),
            ),
        ),
        (
            "docker_client",
            lambda probe: probe.results.__setitem__(
                (preflight.DOCKER_BINARY, "--version"), CommandResult(127)
            ),
        ),
        (
            "docker_compose",
            lambda probe: probe.results.__setitem__(
                (preflight.DOCKER_BINARY, "compose", "version", "--short"),
                CommandResult(0, "1.29.2\n"),
            ),
        ),
        (
            "docker_root_dir",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_ROOT_INFO_COMMAND,
                CommandResult(0, json.dumps("/srv/docker") + "\n"),
            ),
        ),
        (
            "docker_storage_backend",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_DRIVER_STATUS_COMMAND,
                CommandResult(
                    0,
                    json.dumps([["driver-type", "io.containerd.snapshotter.v1"]]) + "\n",
                ),
            ),
        ),
        (
            "docker_logging_driver",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_LOGGING_DRIVER_COMMAND,
                CommandResult(0, json.dumps("local") + "\n"),
            ),
        ),
        (
            "docker_cgroup_version",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_CGROUP_VERSION_COMMAND,
                CommandResult(0, json.dumps("1") + "\n"),
            ),
        ),
        (
            "docker_cgroup_driver",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_CGROUP_DRIVER_COMMAND,
                CommandResult(0, json.dumps("cgroupfs") + "\n"),
            ),
        ),
        (
            "openssl",
            lambda probe: probe.results.__setitem__(
                (preflight.OPENSSL_BINARY, "version"), CommandResult(127)
            ),
        ),
        (
            "rsync",
            lambda probe: probe.results.__setitem__(
                (preflight.RSYNC_BINARY, "--version"), CommandResult(127)
            ),
        ),
        (
            "ssh",
            lambda probe: probe.results.__setitem__(
                (preflight.SSH_BINARY, "-V"), CommandResult(127)
            ),
        ),
        (
            "psql",
            lambda probe: probe.results.__setitem__(
                (preflight.PSQL_BINARY, "--version"),
                CommandResult(0, "psql (PostgreSQL) 15.14\n"),
            ),
        ),
        (
            "findmnt",
            lambda probe: probe.results.__setitem__(
                (preflight.FINDMNT_BINARY, "--version"), CommandResult(127)
            ),
        ),
        (
            "lsblk",
            lambda probe: probe.results.__setitem__(
                (preflight.LSBLK_BINARY, "--version"), CommandResult(127)
            ),
        ),
        (
            "blkid",
            lambda probe: probe.results.__setitem__(
                (preflight.BLKID_BINARY, "--version"), CommandResult(127)
            ),
        ),
        (
            "xfs_info",
            lambda probe: probe.results.__setitem__(
                (preflight.XFS_INFO_BINARY, "-V"), CommandResult(127)
            ),
        ),
        (
            "docker_storage_filesystem",
            lambda probe: probe.results.__setitem__(
                preflight.DOCKER_FILESYSTEM_COMMAND,
                CommandResult(0, "btrfs\n"),
            ),
        ),
    ],
)
def test_each_required_baseline_mismatch_fails_closed(
    name: str, mutate: Callable[[FakeProbe], None]
) -> None:
    probe = FakeProbe()
    mutate(probe)

    check = result_by_name(probe, name)

    assert check.passed is False


def test_xfs_docker_mount_requires_ftype_one() -> None:
    valid = FakeProbe()
    valid.results[preflight.DOCKER_FILESYSTEM_COMMAND] = CommandResult(0, "xfs\n")
    valid.results[preflight.XFS_LAYOUT_COMMAND] = CommandResult(
        0,
        "meta-data=/dev/sdb naming =version 2 bsize=4096 ascii-ci=0, ftype=1\n",
    )

    valid_check = result_by_name(valid, "docker_storage_filesystem")

    assert valid_check.payload() == {"passed": True, "version": "xfs-ftype1"}
    assert preflight.XFS_LAYOUT_COMMAND in valid.calls

    invalid = FakeProbe()
    invalid.results[preflight.DOCKER_FILESYSTEM_COMMAND] = CommandResult(0, "xfs\n")
    invalid.results[preflight.XFS_LAYOUT_COMMAND] = CommandResult(
        0,
        "naming =version 2 bsize=4096 ascii-ci=0, ftype=0\n",
    )

    invalid_check = result_by_name(invalid, "docker_storage_filesystem")

    assert invalid_check.payload() == {
        "passed": False,
        "reason": "xfs_ftype_required",
        "version": "xfs",
    }


def test_containerd_image_store_is_reported_as_unapproved() -> None:
    probe = FakeProbe()
    probe.results[preflight.DOCKER_DRIVER_INFO_COMMAND] = CommandResult(
        0, json.dumps("overlayfs") + "\n"
    )
    probe.results[preflight.DOCKER_DRIVER_STATUS_COMMAND] = CommandResult(
        0,
        json.dumps([["driver-type", "io.containerd.snapshotter.v1"]]) + "\n",
    )

    check = result_by_name(probe, "docker_storage_backend")

    assert check.payload() == {
        "passed": False,
        "reason": "containerd_image_store_unapproved",
        "version": "containerd-image-store",
    }


def test_cli_returns_zero_or_one_and_emits_only_bounded_json() -> None:
    success_output = io.StringIO()
    success_error = io.StringIO()

    assert preflight.main([], probe=FakeProbe(), stdout=success_output, stderr=success_error) == 0
    payload = json.loads(success_output.getvalue())
    assert payload["event"] == "production_host_preflight_result"
    assert payload["profile"] == "base"
    assert payload["status"] == "passed"
    assert set(payload["checks"]) == set(BASE_CHECK_NAMES)
    assert success_error.getvalue() == ""

    failed_probe = FakeProbe()
    failed_probe.architecture = "aarch64"
    failure_output = io.StringIO()
    failure_error = io.StringIO()
    assert preflight.main([], probe=failed_probe, stdout=failure_output, stderr=failure_error) == 1
    failure_payload = json.loads(failure_error.getvalue())
    assert failure_payload["status"] == "failed"
    assert failure_payload["checks"]["architecture"] == {
        "passed": False,
        "reason": "architecture_mismatch",
    }
    assert failure_output.getvalue() == ""


def test_cli_error_returns_two_without_running_probe() -> None:
    output = io.StringIO()
    error = io.StringIO()
    probe = FakeProbe()

    assert preflight.main(["--install"], probe=probe, stdout=output, stderr=error) == 2

    assert json.loads(error.getvalue()) == {
        "code": "invalid_arguments",
        "event": "production_host_preflight_result",
        "status": "error",
    }
    assert output.getvalue() == ""
    assert probe.calls == []


def test_runtime_cli_includes_daemon_checks() -> None:
    output = io.StringIO()
    error = io.StringIO()

    assert preflight.main(["runtime"], probe=FakeProbe(), stdout=output, stderr=error) == 0

    payload = json.loads(output.getvalue())
    assert payload["profile"] == "runtime"
    assert set(payload["checks"]) == set(RUNTIME_CHECK_NAMES)
    assert error.getvalue() == ""


def test_raw_probe_output_and_host_metadata_are_never_echoed() -> None:
    secret_markers = (
        "10.23.45.67",
        "prod-operator",
        "/home/prod-operator",
        "super-secret-password",
    )
    probe = FakeProbe()
    probe.os_release += "INTERNAL_OWNER=prod-operator\n"
    probe.results[(preflight.OPENSSL_BINARY, "version")] = CommandResult(
        1,
        stdout="10.23.45.67 /home/prod-operator\n",
        stderr="super-secret-password\n",
    )
    probe.results[preflight.DOCKER_ROOT_INFO_COMMAND] = CommandResult(
        0, json.dumps("/home/prod-operator/docker-10.23.45.67") + "\n"
    )
    probe.results[preflight.DOCKER_DRIVER_INFO_COMMAND] = CommandResult(
        0, json.dumps("super-secret-password") + "\n"
    )
    probe.results[preflight.DOCKER_LOGGING_DRIVER_COMMAND] = CommandResult(
        0, json.dumps("10.23.45.67-prod-operator") + "\n"
    )
    output = io.StringIO()
    error = io.StringIO()

    assert preflight.main(["runtime"], probe=probe, stdout=output, stderr=error) == 1

    serialized = output.getvalue() + error.getvalue()
    assert all(marker not in serialized for marker in secret_markers)
    payload = json.loads(error.getvalue())
    assert payload["checks"]["openssl"] == {
        "passed": False,
        "reason": "openssl_unavailable",
    }


def test_source_contains_no_host_mutation_or_network_primitive() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "subprocess.run(" in source
    assert "shell=False" in source
    for forbidden in (
        "apt-get",
        "apt install",
        "mkfs",
        "wipefs",
        "parted",
        "systemctl enable",
        "systemctl start",
        "docker pull",
        "docker build",
        "git fetch",
        "git pull",
        "urlopen(",
        "requests.",
        "httpx.",
    ):
        assert forbidden not in source.lower()
