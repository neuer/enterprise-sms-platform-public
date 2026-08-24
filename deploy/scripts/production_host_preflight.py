#!/usr/bin/env python3
"""Read-only, fail-closed validation of the production host baseline."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TextIO

OS_RELEASE_PATH = Path("/etc/os-release")
MEMINFO_PATH = Path("/proc/meminfo")
SYSTEMD_RUNTIME_PATH = Path("/run/systemd/system")
COMMAND_TIMEOUT_SECONDS = 5
MAX_OS_RELEASE_BYTES = 64 * 1024
MAX_MEMINFO_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT_CHARS = 64 * 1024

EXPECTED_OS_ID = "ubuntu"
EXPECTED_OS_VERSION = "24.04"
EXPECTED_ARCHITECTURES = frozenset({"amd64", "x86_64"})
EXPECTED_VCPU_COUNT = 12
MINIMUM_MEMORY_KIB = 47 * 1024 * 1024

PYTHON_BINARY = "/usr/bin/python3"
SYSTEMCTL_BINARY = "/usr/bin/systemctl"
TIMEDATECTL_BINARY = "/usr/bin/timedatectl"
DOCKER_BINARY = "/usr/bin/docker"
OPENSSL_BINARY = "/usr/bin/openssl"
RSYNC_BINARY = "/usr/bin/rsync"
SSH_BINARY = "/usr/bin/ssh"
PSQL_BINARY = "/usr/bin/psql"
FINDMNT_BINARY = "/usr/bin/findmnt"
LSBLK_BINARY = "/usr/bin/lsblk"
BLKID_BINARY = "/usr/sbin/blkid"
XFS_INFO_BINARY = "/usr/sbin/xfs_info"
VMWARE_TOOLBOX_BINARY = "/usr/bin/vmware-toolbox-cmd"
DOCKER_ROOT_PATH = "/var/lib/docker"
DOCKER_ROOT_INFO_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .DockerRootDir}}",
)
DOCKER_DRIVER_INFO_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .Driver}}",
)
DOCKER_DRIVER_STATUS_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .DriverStatus}}",
)
DOCKER_LOGGING_DRIVER_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .LoggingDriver}}",
)
DOCKER_CGROUP_VERSION_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .CgroupVersion}}",
)
DOCKER_CGROUP_DRIVER_COMMAND = (
    DOCKER_BINARY,
    "info",
    "--format",
    "{{json .CgroupDriver}}",
)
DOCKER_FILESYSTEM_COMMAND = (
    FINDMNT_BINARY,
    "--noheadings",
    "--output",
    "FSTYPE",
    "--target",
    DOCKER_ROOT_PATH,
)
XFS_LAYOUT_COMMAND = (XFS_INFO_BINARY, DOCKER_ROOT_PATH)
OPEN_VM_TOOLS_SERVICE_COMMAND = (
    SYSTEMCTL_BINARY,
    "is-active",
    "--quiet",
    "open-vm-tools.service",
)

RESULT_EVENT = "production_host_preflight_result"
HostProfile = Literal["base", "runtime"]
HOST_PROFILES: tuple[HostProfile, ...] = ("base", "runtime")
BASE_CHECK_NAMES = (
    "operating_system",
    "architecture",
    "ga_kernel",
    "guest_vcpu",
    "guest_memory",
    "host_python",
    "systemd",
    "time_synchronized",
    "host_timezone",
    "open_vm_tools",
    "docker_client",
    "docker_compose",
    "openssl",
    "rsync",
    "ssh",
    "psql",
    "findmnt",
    "lsblk",
    "blkid",
    "xfs_info",
    "docker_storage_filesystem",
)
RUNTIME_CHECK_NAMES = (
    *BASE_CHECK_NAMES,
    "docker_root_dir",
    "docker_storage_backend",
    "docker_logging_driver",
    "docker_cgroup_version",
    "docker_cgroup_driver",
)
FAILURE_REASONS = frozenset(
    {
        "operating_system_mismatch",
        "architecture_mismatch",
        "ga_kernel_6_8_required",
        "guest_vcpu_12_required",
        "guest_memory_47_gib_minimum",
        "host_python_3_12_required",
        "systemd_unavailable",
        "systemd_255_required",
        "time_not_synchronized",
        "host_timezone_must_be_asia_shanghai",
        "open_vm_tools_unavailable",
        "open_vm_tools_service_inactive",
        "docker_client_unavailable",
        "docker_compose_v2_required",
        "docker_daemon_unavailable",
        "docker_root_dir_mismatch",
        "containerd_image_store_unapproved",
        "docker_storage_backend_unrecognized",
        "docker_logging_driver_mismatch",
        "docker_cgroup_version_mismatch",
        "docker_cgroup_driver_mismatch",
        "openssl_unavailable",
        "rsync_unavailable",
        "ssh_unavailable",
        "psql_16_required",
        "findmnt_unavailable",
        "lsblk_unavailable",
        "blkid_unavailable",
        "xfs_info_unavailable",
        "docker_filesystem_unavailable",
        "docker_filesystem_unsupported",
        "xfs_ftype_required",
    }
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded command outcome used by the injectable host probe."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class HostProbe(Protocol):
    """Read-only observations required by the production host preflight."""

    def read_text(self, path: Path, maximum_bytes: int) -> str: ...

    def machine(self) -> str: ...

    def kernel_release(self) -> str: ...

    def cpu_count(self) -> int | None: ...

    def is_directory(self, path: Path) -> bool: ...

    def run(self, argv: Sequence[str]) -> CommandResult: ...


class LocalHostProbe:
    """Production probe implementation; every operation is read-only."""

    def read_text(self, path: Path, maximum_bytes: int) -> str:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            value = stream.read(maximum_bytes + 1)
        if len(value.encode("utf-8")) > maximum_bytes:
            raise ValueError("host metadata exceeds its bounded size")
        return value

    def machine(self) -> str:
        return platform.machine()

    def kernel_release(self) -> str:
        return platform.release()

    def cpu_count(self) -> int | None:
        return os.cpu_count()

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def run(self, argv: Sequence[str]) -> CommandResult:
        try:
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env={
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                },
            )
        except FileNotFoundError:
            return CommandResult(returncode=127)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124)
        except OSError:
            return CommandResult(returncode=126)
        if (
            len(completed.stdout) > MAX_COMMAND_OUTPUT_CHARS
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_CHARS
        ):
            return CommandResult(returncode=125)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One bounded check result suitable for safe JSON output."""

    name: str
    passed: bool
    version: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.passed == (self.reason is not None):
            raise ValueError("a failed check requires exactly one fixed reason")
        if self.reason is not None and self.reason not in FAILURE_REASONS:
            raise ValueError("the failure reason is outside the fixed contract")

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {"passed": self.passed}
        if self.version is not None:
            result["version"] = self.version
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True, slots=True)
class HostPreflightReport:
    """Complete production host baseline result."""

    profile: HostProfile
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        expected = BASE_CHECK_NAMES if self.profile == "base" else RUNTIME_CHECK_NAMES
        return tuple(check.name for check in self.checks) == expected and all(
            check.passed for check in self.checks
        )

    def payload(self) -> dict[str, object]:
        return {
            "checks": {check.name: check.payload() for check in self.checks},
            "event": RESULT_EVENT,
            "profile": self.profile,
            "status": "passed" if self.passed else "failed",
        }


def _parse_os_release(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        candidate = raw_value.strip()
        if (
            len(candidate) >= 2
            and candidate[0] in {"'", '"'}
            and candidate[-1] == candidate[0]
        ):
            candidate = candidate[1:-1]
        fields[key.strip()] = candidate
    return fields


def _combined_output(result: CommandResult) -> str:
    return "\n".join((result.stdout, result.stderr))


def _matched_version(result: CommandResult, pattern: re.Pattern[str]) -> str | None:
    if result.returncode != 0:
        return None
    match = pattern.search(_combined_output(result))
    return match.group(1) if match is not None else None


def _version_check(
    probe: HostProbe,
    *,
    name: str,
    argv: Sequence[str],
    pattern: re.Pattern[str],
    failure_reason: str,
    required_prefix: tuple[int, ...] | None = None,
) -> CheckResult:
    result = probe.run(argv)
    version = _matched_version(result, pattern)
    passed = version is not None
    if version is not None and required_prefix is not None:
        numeric_prefix = tuple(
            int(part) for part in version.split(".")[: len(required_prefix)]
        )
        passed = numeric_prefix == required_prefix
    return CheckResult(
        name=name,
        passed=passed,
        version=version,
        reason=None if passed else failure_reason,
    )


def _inspect_operating_system(probe: HostProbe) -> CheckResult:
    try:
        fields = _parse_os_release(
            probe.read_text(OS_RELEASE_PATH, MAX_OS_RELEASE_BYTES)
        )
    except (OSError, UnicodeError, ValueError):
        return CheckResult(
            name="operating_system",
            passed=False,
            reason="operating_system_mismatch",
        )
    passed = (
        fields.get("ID", "").lower() == EXPECTED_OS_ID
        and fields.get("VERSION_ID") == EXPECTED_OS_VERSION
    )
    return CheckResult(
        name="operating_system",
        passed=passed,
        version=EXPECTED_OS_VERSION if passed else None,
        reason=None if passed else "operating_system_mismatch",
    )


def _inspect_architecture(probe: HostProbe) -> CheckResult:
    try:
        architecture = probe.machine().strip().lower()
    except (OSError, ValueError):
        return CheckResult(
            name="architecture", passed=False, reason="architecture_mismatch"
        )
    passed = architecture in EXPECTED_ARCHITECTURES
    return CheckResult(
        name="architecture",
        passed=passed,
        version="amd64" if passed else None,
        reason=None if passed else "architecture_mismatch",
    )


def _inspect_ga_kernel(probe: HostProbe) -> CheckResult:
    try:
        release = probe.kernel_release().strip()
    except (OSError, ValueError):
        release = ""
    match = re.fullmatch(r"(6\.8\.[0-9]+-[0-9]+-generic)", release)
    passed = match is not None
    return CheckResult(
        name="ga_kernel",
        passed=passed,
        version=match.group(1) if match is not None else None,
        reason=None if passed else "ga_kernel_6_8_required",
    )


def _inspect_guest_vcpu(probe: HostProbe) -> CheckResult:
    try:
        count = probe.cpu_count()
    except (OSError, ValueError):
        count = None
    passed = count == EXPECTED_VCPU_COUNT
    return CheckResult(
        name="guest_vcpu",
        passed=passed,
        version=str(EXPECTED_VCPU_COUNT) if passed else None,
        reason=None if passed else "guest_vcpu_12_required",
    )


def _inspect_guest_memory(probe: HostProbe) -> CheckResult:
    try:
        meminfo = probe.read_text(MEMINFO_PATH, MAX_MEMINFO_BYTES)
    except (OSError, UnicodeError, ValueError):
        meminfo = ""
    matches = re.findall(r"(?m)^MemTotal:\s+([0-9]+)\s+kB\s*$", meminfo)
    total_kib = int(matches[0]) if len(matches) == 1 else 0
    passed = total_kib >= MINIMUM_MEMORY_KIB
    return CheckResult(
        name="guest_memory",
        passed=passed,
        version="48GiB-class" if passed else None,
        reason=None if passed else "guest_memory_47_gib_minimum",
    )


def _inspect_systemd(probe: HostProbe) -> CheckResult:
    try:
        runtime_available = probe.is_directory(SYSTEMD_RUNTIME_PATH)
    except OSError:
        runtime_available = False
    result = probe.run((SYSTEMCTL_BINARY, "--version"))
    version = _matched_version(result, re.compile(r"(?m)^systemd\s+([0-9]+)\b"))
    passed = runtime_available and version == "255"
    reason = (
        None
        if passed
        else "systemd_unavailable"
        if not runtime_available or version is None
        else "systemd_255_required"
    )
    return CheckResult(
        name="systemd",
        passed=passed,
        version=version,
        reason=reason,
    )


def _inspect_time_sync(probe: HostProbe) -> CheckResult:
    result = probe.run(
        (
            TIMEDATECTL_BINARY,
            "show",
            "--property=NTPSynchronized",
            "--value",
        )
    )
    synchronized = result.returncode == 0 and result.stdout.strip().lower() == "yes"
    return CheckResult(
        name="time_synchronized",
        passed=synchronized,
        reason=None if synchronized else "time_not_synchronized",
    )


def _inspect_host_timezone(probe: HostProbe) -> CheckResult:
    result = probe.run(
        (
            TIMEDATECTL_BINARY,
            "show",
            "--property=Timezone",
            "--value",
        )
    )
    is_shanghai = result.returncode == 0 and result.stdout.strip() == "Asia/Shanghai"
    return CheckResult(
        name="host_timezone",
        passed=is_shanghai,
        version="Asia/Shanghai" if is_shanghai else None,
        reason=None if is_shanghai else "host_timezone_must_be_asia_shanghai",
    )


def _inspect_open_vm_tools(probe: HostProbe) -> CheckResult:
    version = _matched_version(
        probe.run((VMWARE_TOOLBOX_BINARY, "-v")),
        re.compile(r"(?m)^([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"),
    )
    service = probe.run(OPEN_VM_TOOLS_SERVICE_COMMAND)
    passed = version is not None and service.returncode == 0
    reason = (
        None
        if passed
        else "open_vm_tools_unavailable"
        if version is None
        else "open_vm_tools_service_inactive"
    )
    return CheckResult(
        name="open_vm_tools",
        passed=passed,
        version=version,
        reason=reason,
    )


def _json_string(result: CommandResult) -> str | None:
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        decoded: object = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return decoded if isinstance(decoded, str) else None


def _driver_status(result: CommandResult) -> tuple[tuple[str, str], ...] | None:
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        decoded: object = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if decoded is None:
        return ()
    if not isinstance(decoded, list):
        return None
    normalized: list[tuple[str, str]] = []
    for item in decoded:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            return None
        normalized.append((item[0].strip().lower(), item[1].strip().lower()))
    return tuple(normalized)


def _docker_runtime_value_check(
    probe: HostProbe,
    *,
    name: str,
    command: Sequence[str],
    expected: str,
    mismatch_reason: str,
) -> CheckResult:
    value = _json_string(probe.run(command))
    passed = value == expected
    return CheckResult(
        name=name,
        passed=passed,
        version=expected if passed else None,
        reason=(
            None
            if passed
            else "docker_daemon_unavailable"
            if value is None
            else mismatch_reason
        ),
    )


def _inspect_docker_daemon(
    probe: HostProbe,
) -> tuple[CheckResult, CheckResult, CheckResult, CheckResult, CheckResult]:
    root = _json_string(probe.run(DOCKER_ROOT_INFO_COMMAND))
    root_passed = root == DOCKER_ROOT_PATH
    root_reason = (
        None
        if root_passed
        else "docker_daemon_unavailable"
        if root is None
        else "docker_root_dir_mismatch"
    )
    root_check = CheckResult(
        name="docker_root_dir",
        passed=root_passed,
        version="approved" if root_passed else None,
        reason=root_reason,
    )

    driver = _json_string(probe.run(DOCKER_DRIVER_INFO_COMMAND))
    statuses = _driver_status(probe.run(DOCKER_DRIVER_STATUS_COMMAND))
    if driver is None or statuses is None:
        backend_check = CheckResult(
            name="docker_storage_backend",
            passed=False,
            reason="docker_daemon_unavailable",
        )
    elif (
        driver.strip().lower() == "overlayfs"
        or (
            "driver-type",
            "io.containerd.snapshotter.v1",
        )
        in statuses
    ):
        backend_check = CheckResult(
            name="docker_storage_backend",
            passed=False,
            version="containerd-image-store",
            reason="containerd_image_store_unapproved",
        )
    elif driver.strip().lower() == "overlay2":
        backend_check = CheckResult(
            name="docker_storage_backend",
            passed=True,
            version="overlay2-classic",
        )
    else:
        backend_check = CheckResult(
            name="docker_storage_backend",
            passed=False,
            reason="docker_storage_backend_unrecognized",
        )
    return (
        root_check,
        backend_check,
        _docker_runtime_value_check(
            probe,
            name="docker_logging_driver",
            command=DOCKER_LOGGING_DRIVER_COMMAND,
            expected="json-file",
            mismatch_reason="docker_logging_driver_mismatch",
        ),
        _docker_runtime_value_check(
            probe,
            name="docker_cgroup_version",
            command=DOCKER_CGROUP_VERSION_COMMAND,
            expected="2",
            mismatch_reason="docker_cgroup_version_mismatch",
        ),
        _docker_runtime_value_check(
            probe,
            name="docker_cgroup_driver",
            command=DOCKER_CGROUP_DRIVER_COMMAND,
            expected="systemd",
            mismatch_reason="docker_cgroup_driver_mismatch",
        ),
    )


def _inspect_docker_filesystem(probe: HostProbe) -> CheckResult:
    filesystem_result = probe.run(DOCKER_FILESYSTEM_COMMAND)
    filesystem_lines = [
        line.strip().lower()
        for line in filesystem_result.stdout.splitlines()
        if line.strip()
    ]
    if filesystem_result.returncode != 0 or len(filesystem_lines) != 1:
        return CheckResult(
            name="docker_storage_filesystem",
            passed=False,
            reason="docker_filesystem_unavailable",
        )
    filesystem = filesystem_lines[0]
    if filesystem == "ext4":
        return CheckResult(
            name="docker_storage_filesystem", passed=True, version="ext4"
        )
    if filesystem != "xfs":
        return CheckResult(
            name="docker_storage_filesystem",
            passed=False,
            reason="docker_filesystem_unsupported",
        )

    xfs_result = probe.run(XFS_LAYOUT_COMMAND)
    ftype_values = re.findall(
        r"(?:^|[,\s])ftype=([01])(?=$|[,\s])",
        xfs_result.stdout,
    )
    ftype_is_valid = xfs_result.returncode == 0 and ftype_values == ["1"]
    return CheckResult(
        name="docker_storage_filesystem",
        passed=ftype_is_valid,
        version="xfs-ftype1" if ftype_is_valid else "xfs",
        reason=None if ftype_is_valid else "xfs_ftype_required",
    )


def inspect_host(
    probe: HostProbe, *, profile: HostProfile = "base"
) -> HostPreflightReport:
    """检查生产主机的固定只读基线，不安装、联网或启动任何服务。"""

    base_checks = (
        _inspect_operating_system(probe),
        _inspect_architecture(probe),
        _inspect_ga_kernel(probe),
        _inspect_guest_vcpu(probe),
        _inspect_guest_memory(probe),
        _version_check(
            probe,
            name="host_python",
            argv=(PYTHON_BINARY, "--version"),
            pattern=re.compile(r"(?m)^Python\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"),
            failure_reason="host_python_3_12_required",
            required_prefix=(3, 12),
        ),
        _inspect_systemd(probe),
        _inspect_time_sync(probe),
        _inspect_host_timezone(probe),
        _inspect_open_vm_tools(probe),
        _version_check(
            probe,
            name="docker_client",
            argv=(DOCKER_BINARY, "--version"),
            pattern=re.compile(
                r"(?m)^Docker version\s+v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="docker_client_unavailable",
        ),
        _version_check(
            probe,
            name="docker_compose",
            argv=(DOCKER_BINARY, "compose", "version", "--short"),
            pattern=re.compile(r"(?m)^v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)\s*$"),
            failure_reason="docker_compose_v2_required",
            required_prefix=(2,),
        ),
        _version_check(
            probe,
            name="openssl",
            argv=(OPENSSL_BINARY, "version"),
            pattern=re.compile(r"(?m)^OpenSSL\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"),
            failure_reason="openssl_unavailable",
        ),
        _version_check(
            probe,
            name="rsync",
            argv=(RSYNC_BINARY, "--version"),
            pattern=re.compile(
                r"(?mi)^rsync\s+version\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="rsync_unavailable",
        ),
        _version_check(
            probe,
            name="ssh",
            argv=(SSH_BINARY, "-V"),
            pattern=re.compile(r"(?m)^OpenSSH_([0-9]+\.[0-9]+(?:p[0-9]+)?)\b"),
            failure_reason="ssh_unavailable",
        ),
        _version_check(
            probe,
            name="psql",
            argv=(PSQL_BINARY, "--version"),
            pattern=re.compile(r"(?m)^psql \(PostgreSQL\)\s+([0-9]+(?:\.[0-9]+)?)\b"),
            failure_reason="psql_16_required",
            required_prefix=(16,),
        ),
        _version_check(
            probe,
            name="findmnt",
            argv=(FINDMNT_BINARY, "--version"),
            pattern=re.compile(
                r"(?m)^findmnt from util-linux\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="findmnt_unavailable",
        ),
        _version_check(
            probe,
            name="lsblk",
            argv=(LSBLK_BINARY, "--version"),
            pattern=re.compile(
                r"(?m)^lsblk from util-linux\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="lsblk_unavailable",
        ),
        _version_check(
            probe,
            name="blkid",
            argv=(BLKID_BINARY, "--version"),
            pattern=re.compile(
                r"(?m)^blkid from util-linux\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="blkid_unavailable",
        ),
        _version_check(
            probe,
            name="xfs_info",
            argv=(XFS_INFO_BINARY, "-V"),
            pattern=re.compile(
                r"(?m)^xfs_info version\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b"
            ),
            failure_reason="xfs_info_unavailable",
        ),
        _inspect_docker_filesystem(probe),
    )
    checks = (
        base_checks
        if profile == "base"
        else (*base_checks, *_inspect_docker_daemon(probe))
    )
    expected = BASE_CHECK_NAMES if profile == "base" else RUNTIME_CHECK_NAMES
    if tuple(check.name for check in checks) != expected:
        raise RuntimeError("production host check contract is inconsistent")
    return HostPreflightReport(profile=profile, checks=checks)


def _emit(payload: Mapping[str, object], stream: TextIO) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    probe: HostProbe | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    if not arguments or arguments == ["base"]:
        profile: HostProfile = "base"
    elif arguments == ["runtime"]:
        profile = "runtime"
    else:
        _emit(
            {
                "code": "invalid_arguments",
                "event": RESULT_EVENT,
                "status": "error",
            },
            error_output,
        )
        return 2
    try:
        report = inspect_host(
            LocalHostProbe() if probe is None else probe,
            profile=profile,
        )
    except Exception:
        _emit(
            {
                "code": "unexpected_preflight_failure",
                "checks": {},
                "event": RESULT_EVENT,
                "profile": profile,
                "status": "failed",
            },
            error_output,
        )
        return 1
    _emit(report.payload(), output if report.passed else error_output)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
