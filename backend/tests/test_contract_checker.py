from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def load_checker() -> ModuleType:
    path = ROOT / "scripts/check_contract.py"
    spec = importlib.util.spec_from_file_location("contract_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def operation_doc(path: str = "/api/v1/example") -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "1"},
        "paths": {path: {"get": {"responses": {"200": {"description": "ok"}}}}},
    }


def test_allow_missing_still_rejects_extra_and_field_differences() -> None:
    module = load_checker()
    expected = operation_doc()

    missing, extra, diffs = module.compare_documents(expected, {"paths": {}}, allow_missing=True)
    assert missing == []
    assert extra == []
    assert diffs == []

    missing, extra, diffs = module.compare_documents(
        expected,
        operation_doc("/api/v1/extra"),
        allow_missing=True,
    )
    assert missing == []
    assert extra == [("/api/v1/extra", "get")]
    assert diffs == []


def test_full_contract_reports_missing_operations() -> None:
    module = load_checker()

    missing, extra, diffs = module.compare_documents(
        operation_doc(),
        {"paths": {}},
        allow_missing=False,
    )

    assert missing == [("/api/v1/example", "get")]
    assert extra == []
    assert diffs == []


def test_checker_cli_imports_app_from_backend_working_directory() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_contract.py"),
            str(ROOT / "openapi.yaml"),
            "--allow-missing",
        ],
        cwd=BACKEND,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "阶段子集模式" in result.stdout


def test_frequency_override_contract_is_closed_and_explicit() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    schema = document["components"]["schemas"]["FrequencyOverride"]

    assert schema["additionalProperties"] is False
    assert schema["properties"] == {
        "verify_per_minute": {"type": "integer", "minimum": 1, "maximum": 100},
        "verify_per_day": {"type": "integer", "minimum": 1, "maximum": 10_000},
        "market_per_day": {"type": "integer", "minimum": 1, "maximum": 1_000},
    }
    for method in ("post",):
        freq_override = document["paths"]["/api/v1/web/admin/apps"][method]["requestBody"][
            "content"
        ]["application/json"]["schema"]["properties"]["freq_override"]
        assert freq_override["anyOf"][0] == {"$ref": "#/components/schemas/FrequencyOverride"}
    update_override = document["paths"]["/api/v1/web/admin/apps/{id}"]["put"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]["freq_override"]
    assert update_override["anyOf"][0] == {"$ref": "#/components/schemas/FrequencyOverride"}


def test_batch_mutation_contract_declares_authentication_and_authorization_failures() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text())

    for suffix in ("cancel", "reschedule"):
        responses = document["paths"][f"/api/v1/messages/batches/{{batch_no}}/{suffix}"]["post"][
            "responses"
        ]
        assert "401" in responses
        assert "403" in responses


def test_controlled_api_uat_contract_is_single_notice_and_idempotent() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    operation = document["paths"]["/api/v1/messages/uat-send"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert operation["security"] == [{"ApiKeyAuth": []}]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"category", "mobiles", "content", "biz_id"}
    assert schema["properties"]["category"]["const"] == "notice"
    assert schema["properties"]["mobiles"]["minItems"] == 1
    assert schema["properties"]["mobiles"]["maxItems"] == 1
    assert schema["properties"]["content"]["minLength"] == 1
    assert schema["properties"]["biz_id"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 32,
    }
    response_statuses = {
        "queued",
        "scheduled",
        "sending",
        "completed",
        "cancelled",
        "balance_blocked",
    }
    assert set(response_schema["properties"]["status"]["enum"]) == response_statuses
    ordinary_response = document["paths"]["/api/v1/messages/send"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert set(ordinary_response["properties"]["status"]["enum"]) == response_statuses
    assert "503" in document["paths"]["/api/v1/messages/send"]["post"]["responses"]
    assert "503" in document["paths"]["/api/v1/web/messages/send"]["post"]["responses"]
    assert {"400", "401", "403", "409", "422", "429", "503"}.issubset(operation["responses"])
