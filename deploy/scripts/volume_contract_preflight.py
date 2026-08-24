#!/usr/bin/env python3
"""Read-only validation for production Docker named-volume metadata.

The storage layout preflight validates the host mounts and fixed directories.  This
companion check closes the other half of the boundary: an existing Docker volume
with the right name must still point at the approved directory and must have been
created for the ``sms-platform`` Compose project.  Missing volumes are intentionally
accepted because Compose creates them from the production storage override later.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

PROJECT_NAME = "sms-platform"
OUTPUT_LIMIT_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class VolumeRequirement:
    logical_name: str
    device: str

    @property
    def name(self) -> str:
        return f"{PROJECT_NAME}_{self.logical_name}"

    @property
    def options(self) -> dict[str, str]:
        return {"type": "none", "o": "bind", "device": self.device}


VOLUME_REQUIREMENTS = (
    VolumeRequirement("pgdata", "/var/lib/sms-platform/postgres/pgdata"),
    VolumeRequirement("redisdata", "/var/lib/sms-platform/redis/broker"),
    VolumeRequirement("redisauthdata", "/var/lib/sms-platform/redis/auth"),
    VolumeRequirement("rediscontroldata", "/var/lib/sms-platform/redis/control"),
    VolumeRequirement("importdata", "/var/lib/sms-platform/runtime/imports"),
    VolumeRequirement("exportdata", "/var/lib/sms-platform/runtime/exports"),
    VolumeRequirement("rawspill", "/var/lib/sms-platform/runtime/raw-spill"),
)
REQUIREMENTS_BY_NAME = {requirement.name: requirement for requirement in VOLUME_REQUIREMENTS}

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"
OPTIONAL_COMPOSE_LABELS = frozenset(
    {
        "com.docker.compose.config-hash",
        "com.docker.compose.version",
    }
)
ALLOWED_COMPOSE_LABELS = frozenset(
    {COMPOSE_PROJECT_LABEL, COMPOSE_VOLUME_LABEL, *OPTIONAL_COMPOSE_LABELS}
)


class VolumeContractError(RuntimeError):
    """A fail-closed contract violation with bounded, non-sensitive context."""

    def __init__(self, code: str, *, volume: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.volume = volume


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True, slots=True)
class VolumeContractReport:
    existing: tuple[str, ...]
    absent: tuple[str, ...]


def _run_command(argv: Sequence[str]) -> CommandResult:
    """Run a fixed Docker argv without a shell and bound execution time."""

    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed docker argv, never a shell
            tuple(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except FileNotFoundError as error:
        raise VolumeContractError("docker_unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise VolumeContractError("docker_timeout") from error
    except OSError as error:
        raise VolumeContractError("docker_execution_failed") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _validate_command_output(result: CommandResult, *, operation: str) -> None:
    if result.returncode != 0:
        raise VolumeContractError(f"docker_{operation}_failed")
    if "\x00" in result.stdout or "\x00" in result.stderr:
        raise VolumeContractError(f"docker_{operation}_invalid_output")
    if (
        len(result.stdout.encode("utf-8")) > OUTPUT_LIMIT_BYTES
        or len(result.stderr.encode("utf-8")) > OUTPUT_LIMIT_BYTES
    ):
        raise VolumeContractError(f"docker_{operation}_output_too_large")


def parse_volume_names(output: str) -> frozenset[str]:
    """Parse the bounded ``docker volume ls --quiet`` output."""

    names: set[str] = set()
    for raw_line in output.splitlines():
        name = raw_line.strip()
        if not name:
            continue
        if any(character.isspace() for character in name):
            raise VolumeContractError("invalid_volume_list")
        if name in names:
            raise VolumeContractError("duplicate_volume_list_entry", volume=name)
        names.add(name)
    return frozenset(names)


def parse_inspection_output(output: str) -> list[Mapping[str, object]]:
    """Decode Docker inspection JSON while rejecting ambiguous structures."""

    try:
        decoded = cast(object, json.loads(output))
    except json.JSONDecodeError as error:
        raise VolumeContractError("invalid_inspection_json") from error
    if not isinstance(decoded, list):
        raise VolumeContractError("invalid_inspection_shape")

    records: list[Mapping[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict) or not all(
            isinstance(key, str) for key in item
        ):
            raise VolumeContractError("invalid_inspection_object")
        records.append(cast(dict[str, object], item))
    return records


def _string_mapping(
    value: object,
    *,
    code: str,
    volume: str,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise VolumeContractError(code, volume=volume)
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise VolumeContractError(code, volume=volume)
        result[raw_key] = raw_value
    return result


def _validate_labels(
    value: object,
    *,
    requirement: VolumeRequirement,
) -> None:
    labels = _string_mapping(
        value,
        code="invalid_labels",
        volume=requirement.name,
    )
    if set(labels) - ALLOWED_COMPOSE_LABELS:
        raise VolumeContractError("unexpected_label", volume=requirement.name)
    if labels.get(COMPOSE_PROJECT_LABEL) != PROJECT_NAME:
        raise VolumeContractError("project_label_mismatch", volume=requirement.name)
    if labels.get(COMPOSE_VOLUME_LABEL) != requirement.logical_name:
        raise VolumeContractError("volume_label_mismatch", volume=requirement.name)
    for label in OPTIONAL_COMPOSE_LABELS & labels.keys():
        if not labels[label].strip():
            raise VolumeContractError("invalid_optional_label", volume=requirement.name)


def validate_inspected_volumes(
    records: Sequence[Mapping[str, object]],
    expected_names: frozenset[str],
) -> None:
    """Validate inspection objects against the fixed production contract."""

    inspected: dict[str, Mapping[str, object]] = {}
    for record in records:
        name = record.get("Name")
        if not isinstance(name, str) or not name:
            raise VolumeContractError("missing_volume_name")
        if name in inspected:
            raise VolumeContractError("duplicate_inspection_object", volume=name)
        if name not in expected_names:
            raise VolumeContractError("unexpected_inspection_object", volume=name)
        inspected[name] = record

    missing = expected_names - inspected.keys()
    if missing:
        raise VolumeContractError("missing_inspection_object", volume=sorted(missing)[0])

    for name in sorted(expected_names):
        requirement = REQUIREMENTS_BY_NAME[name]
        record = inspected[name]
        if record.get("Driver") != "local":
            raise VolumeContractError("driver_mismatch", volume=name)
        if record.get("Scope") != "local":
            raise VolumeContractError("scope_mismatch", volume=name)
        options = _string_mapping(
            record.get("Options"),
            code="invalid_options",
            volume=name,
        )
        if options != requirement.options:
            raise VolumeContractError("options_mismatch", volume=name)
        _validate_labels(record.get("Labels"), requirement=requirement)


def inspect_volume_contract(
    runner: CommandRunner = _run_command,
) -> VolumeContractReport:
    """Inspect existing project volumes without creating or modifying any volume."""

    list_result = runner(
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"name={PROJECT_NAME}_",
        )
    )
    _validate_command_output(list_result, operation="volume_list")
    listed_names = parse_volume_names(list_result.stdout)
    unexpected = listed_names - REQUIREMENTS_BY_NAME.keys()
    if unexpected:
        raise VolumeContractError("unexpected_project_volume", volume=sorted(unexpected)[0])

    existing = frozenset(REQUIREMENTS_BY_NAME) & listed_names
    if existing:
        inspect_result = runner(
            ("docker", "volume", "inspect", *sorted(existing))
        )
        _validate_command_output(inspect_result, operation="volume_inspect")
        records = parse_inspection_output(inspect_result.stdout)
        validate_inspected_volumes(records, existing)

    absent = frozenset(REQUIREMENTS_BY_NAME) - existing
    return VolumeContractReport(tuple(sorted(existing)), tuple(sorted(absent)))


def _emit_success(report: VolumeContractReport) -> None:
    print(
        json.dumps(
            {
                "event": "volume_contract_preflight_result",
                "status": "passed",
                "existing": len(report.existing),
                "absent": len(report.absent),
            },
            sort_keys=True,
        )
    )


def _emit_failure(error: VolumeContractError) -> None:
    payload: dict[str, object] = {
        "event": "volume_contract_preflight_result",
        "status": "failed",
        "code": error.code,
    }
    if error.volume is not None:
        payload["volume"] = error.volume
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def main() -> int:
    try:
        report = inspect_volume_contract()
    except VolumeContractError as error:
        _emit_failure(error)
        return 1
    _emit_success(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
