from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import volume_contract_preflight as preflight  # noqa: E402
from volume_contract_preflight import (  # noqa: E402
    CommandResult,
    VolumeContractError,
    inspect_volume_contract,
    validate_inspected_volumes,
)

SCRIPT = ROOT / "deploy" / "scripts" / "volume_contract_preflight.py"


class FakeRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(argv))
        if not self._responses:
            raise AssertionError("unexpected command")
        return self._responses.pop(0)


def valid_record(name: str) -> dict[str, object]:
    requirement = preflight.REQUIREMENTS_BY_NAME[name]
    return {
        "Name": name,
        "Driver": "local",
        "Scope": "local",
        "Options": requirement.options,
        "Labels": {
            "com.docker.compose.project": "sms-platform",
            "com.docker.compose.volume": requirement.logical_name,
            "com.docker.compose.config-hash": "bounded-config-hash",
            "com.docker.compose.version": "2.39.1",
        },
        # Docker may add unrelated top-level observation fields across versions.
        "CreatedAt": "2026-08-24T00:00:00+08:00",
        "Mountpoint": "/var/lib/docker/volumes/redacted/_data",
    }


def result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> CommandResult:
    return CommandResult(returncode, stdout, stderr)


def assert_error(
    code: str,
    records: Sequence[Mapping[str, object]],
    names: frozenset[str],
) -> None:
    with pytest.raises(VolumeContractError) as caught:
        validate_inspected_volumes(records, names)
    assert caught.value.code == code


def test_contract_has_exact_project_names_and_fixed_devices() -> None:
    assert {
        requirement.name: requirement.device
        for requirement in preflight.VOLUME_REQUIREMENTS
    } == {
        "sms-platform_pgdata": "/var/lib/sms-platform/postgres/pgdata",
        "sms-platform_redisdata": "/var/lib/sms-platform/redis/broker",
        "sms-platform_redisauthdata": "/var/lib/sms-platform/redis/auth",
        "sms-platform_rediscontroldata": "/var/lib/sms-platform/redis/control",
        "sms-platform_importdata": "/var/lib/sms-platform/runtime/imports",
        "sms-platform_exportdata": "/var/lib/sms-platform/runtime/exports",
        "sms-platform_rawspill": "/var/lib/sms-platform/runtime/raw-spill",
    }


def test_all_missing_volumes_are_allowed_for_later_compose_creation() -> None:
    runner = FakeRunner([result()])

    report = inspect_volume_contract(runner)

    assert report.existing == ()
    assert report.absent == tuple(sorted(preflight.REQUIREMENTS_BY_NAME))
    assert runner.calls == [
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            "name=sms-platform_",
        )
    ]


def test_existing_subset_is_inspected_once_with_safe_fixed_argv() -> None:
    names = ("sms-platform_pgdata", "sms-platform_redisdata")
    runner = FakeRunner(
        [
            result(stdout="\n".join(reversed(names)) + "\n"),
            result(stdout=json.dumps([valid_record(name) for name in names])),
        ]
    )

    report = inspect_volume_contract(runner)

    assert report.existing == names
    assert runner.calls[1] == ("docker", "volume", "inspect", *names)
    assert all(isinstance(argument, str) for call in runner.calls for argument in call)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("Driver", "nfs", "driver_mismatch"),
        ("Driver", None, "driver_mismatch"),
        ("Scope", "global", "scope_mismatch"),
        ("Scope", None, "scope_mismatch"),
    ],
)
def test_driver_and_scope_must_be_present_and_exact(
    field: str,
    value: object,
    code: str,
) -> None:
    name = "sms-platform_pgdata"
    record = valid_record(name)
    if value is None:
        record.pop(field)
    else:
        record[field] = value

    assert_error(code, [record], frozenset({name}))


@pytest.mark.parametrize(
    "options",
    [
        {"type": "none", "device": "/var/lib/sms-platform/postgres/pgdata"},
        {
            "type": "none",
            "o": "bind",
            "device": "/var/lib/sms-platform/postgres/wrong",
        },
        {
            "type": "none",
            "o": "bind",
            "device": "/var/lib/sms-platform/postgres/pgdata",
            "uid": "70",
        },
        None,
    ],
)
def test_options_reject_missing_extra_wrong_device_and_missing_field(
    options: object,
) -> None:
    name = "sms-platform_pgdata"
    record = valid_record(name)
    if options is None:
        record.pop("Options")
    else:
        record["Options"] = options

    expected_code = "invalid_options" if options is None else "options_mismatch"
    assert_error(expected_code, [record], frozenset({name}))


@pytest.mark.parametrize(
    ("labels", "code"),
    [
        (None, "invalid_labels"),
        (
            {"com.docker.compose.volume": "pgdata"},
            "project_label_mismatch",
        ),
        (
            {
                "com.docker.compose.project": "other",
                "com.docker.compose.volume": "pgdata",
            },
            "project_label_mismatch",
        ),
        (
            {
                "com.docker.compose.project": "sms-platform",
                "com.docker.compose.volume": "redisdata",
            },
            "volume_label_mismatch",
        ),
        (
            {
                "com.docker.compose.project": "sms-platform",
                "com.docker.compose.volume": "pgdata",
                "owner": "unapproved",
            },
            "unexpected_label",
        ),
        (
            {
                "com.docker.compose.project": "sms-platform",
                "com.docker.compose.volume": "pgdata",
                "com.docker.compose.version": " ",
            },
            "invalid_optional_label",
        ),
    ],
)
def test_compose_labels_are_required_matched_and_bounded(
    labels: object,
    code: str,
) -> None:
    name = "sms-platform_pgdata"
    record = valid_record(name)
    if labels is None:
        record.pop("Labels")
    else:
        record["Labels"] = labels

    assert_error(code, [record], frozenset({name}))


def test_only_required_project_and_volume_labels_are_sufficient() -> None:
    name = "sms-platform_pgdata"
    record = valid_record(name)
    record["Labels"] = {
        "com.docker.compose.project": "sms-platform",
        "com.docker.compose.volume": "pgdata",
    }

    validate_inspected_volumes([record], frozenset({name}))


def test_duplicate_unexpected_and_missing_inspection_objects_fail_closed() -> None:
    name = "sms-platform_pgdata"
    record = valid_record(name)
    assert_error(
        "duplicate_inspection_object",
        [record, record.copy()],
        frozenset({name}),
    )
    assert_error(
        "unexpected_inspection_object",
        [valid_record("sms-platform_redisdata")],
        frozenset({name}),
    )
    assert_error("missing_inspection_object", [], frozenset({name}))


@pytest.mark.parametrize("output", ["not-json", "{}", "[null]", '[{"Name": 3}]'])
def test_invalid_json_and_object_shapes_fail_closed(output: str) -> None:
    runner = FakeRunner(
        [
            result(stdout="sms-platform_pgdata\n"),
            result(stdout=output),
        ]
    )

    with pytest.raises(VolumeContractError):
        inspect_volume_contract(runner)


def test_list_and_inspect_command_failures_do_not_use_partial_output() -> None:
    list_runner = FakeRunner(
        [result(returncode=1, stdout="sms-platform_pgdata\n", stderr="daemon secret")]
    )
    with pytest.raises(VolumeContractError, match="docker_volume_list_failed"):
        inspect_volume_contract(list_runner)

    inspect_runner = FakeRunner(
        [
            result(stdout="sms-platform_pgdata\n"),
            result(returncode=1, stdout="[]", stderr="daemon secret"),
        ]
    )
    with pytest.raises(VolumeContractError, match="docker_volume_inspect_failed"):
        inspect_volume_contract(inspect_runner)


def test_duplicate_and_unexpected_project_volume_list_entries_fail_closed() -> None:
    duplicate = FakeRunner(
        [result(stdout="sms-platform_pgdata\nsms-platform_pgdata\n")]
    )
    with pytest.raises(VolumeContractError, match="duplicate_volume_list_entry"):
        inspect_volume_contract(duplicate)

    unexpected = FakeRunner([result(stdout="sms-platform_legacydata\n")])
    with pytest.raises(VolumeContractError, match="unexpected_project_volume"):
        inspect_volume_contract(unexpected)


def test_main_emits_only_bounded_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leaked = "raw-inspect-secret-must-not-appear"

    def fail() -> preflight.VolumeContractReport:
        raise VolumeContractError("options_mismatch", volume="sms-platform_pgdata")

    monkeypatch.setattr(preflight, "inspect_volume_contract", fail)
    assert preflight.main() == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload == {
        "code": "options_mismatch",
        "event": "volume_contract_preflight_result",
        "status": "failed",
        "volume": "sms-platform_pgdata",
    }
    assert leaked not in captured.out + captured.err


def test_script_contains_no_volume_mutation_or_shell_execution() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert '"volume", "create"' not in source
    assert '"volume", "rm"' not in source
    assert "docker volume create" not in source
    assert "docker volume rm" not in source
