from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from classify_ci_changes import classify_paths  # noqa: E402

CONSOLE_HIGH_RISK_PATHS = (
    "backend/app/api/vendor_test.py",
    "backend/app/main.py",
    "backend/app/services/vendor_control_client.py",
    "backend/app/services/vendor_control_state.py",
    "backend/app/services/vendor_test_operation.py",
    "backend/app/services/vendor_test_operation_repository.py",
    "backend/app/services/vendor_test_recipient.py",
    "backend/app/services/vendor_test_recipient_repository.py",
    "backend/app/services/vendor_test_step_up.py",
    "backend/app/services/vendor_test_uat.py",
    "backend/migrations/versions/0017_vendor_test_web_console.py",
    "backend/migrations/versions/0018_vendor_test_operation_vendor_code.py",
    "backend/migrations/versions/0019_vendor_test_recipient_hmac_alias.py",
    "backend/vendor_control_protocol.py",
    "backend/vendor_control_protocol.pyi",
    "deploy/scripts/install_vendor_credentials.py",
    "deploy/scripts/vendor_control_agent.py",
    "deploy/scripts/vendor_control_reload.py",
    "deploy/scripts/vendor_control_journal.py",
    "deploy/scripts/vendor_control_protocol.py",
    "deploy/scripts/vendor_credential_store.py",
    "deploy/scripts/vendor_seal_sessions.py",
    "deploy/.env.example",
    "deploy/systemd/vendor-control-agent.service",
    "deploy/scripts/install_test_secure_access.py",
    "deploy/scripts/test_secure_access_contract.py",
    "deploy/scripts/test_secure_access_manager.py",
    "deploy/scripts/test_secure_access_runtime.py",
    "deploy/systemd/sms-platform-test-secure-access.service",
    "deploy/sms-compose",
    "scripts/check_invariants.py",
    "scripts/classify_ci_changes.py",
    "frontend/src/api/admin.ts",
    "frontend/src/components/VendorCredentialDialog.vue",
    "frontend/src/components/VendorTestConsole.vue",
    "frontend/src/components/VendorTestRecipientDialog.vue",
    "frontend/src/components/VendorTestUatPanel.vue",
    "frontend/src/lib/vendorSeal.ts",
    "frontend/src/views/ConfigView.vue",
    "openapi.yaml",
    "schema.sql",
)


@pytest.mark.parametrize("path", CONSOLE_HIGH_RISK_PATHS)
def test_console_security_changes_force_backend_frontend_and_g2(path: str) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2) == (True, True, True)
    assert "vendor-live" in result.categories


def test_ci_vendor_gate_is_mock_only_and_installs_locked_frontend_runtime() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    backend_job = workflow.split("  backend-vendor-lint:\n", maxsplit=1)[1].split(
        "\n  backend-coverage:\n", maxsplit=1
    )[0]

    assert 'node-version: "24"' in backend_job
    assert "npm ci" in backend_job
    assert backend_job.index("npm ci") < backend_job.index(
        "npm test --prefix frontend"
    )
    assert "needs.changes.outputs.frontend != 'true'" in backend_job
    assert "verify_vendor_control_uds.sh" not in backend_job
    assert "scripts/run_backend_tests.sh unit" in workflow
    assert "tests/vendor-seal.test.ts" in backend_job
    assert "tests/vendor-test-api.test.ts" in backend_job
    assert "tests/vendor-test-console.test.ts" in backend_job
    for forbidden in (
        "vendor.example.invalid",
        "VENDOR_MOCK: \"0\"",
        "vendor_secret_name",
        "vendor_secret_key",
        "SecretName",
        "SecretKey",
    ):
        assert forbidden not in workflow


def test_api_and_workers_never_mount_the_host_docker_socket() -> None:
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    runtime = compose.split("  api:\n", maxsplit=1)[1].split(
        "\n  mock-vendor:\n", maxsplit=1
    )[0]

    assert "/var/run/docker.sock" not in runtime
    assert "docker.sock" not in runtime
    assert "/run/sms-platform/secrets" not in runtime
    assert runtime.count(
        "${SMS_VENDOR_CONTROL_SOCKET_DIR:-./vendor-control-empty}:"
        "/run/vendor-control:ro"
    ) == 2


def test_template_and_sign_apis_have_no_vendor_credential_or_http_client_path() -> None:
    for relative_path in ("backend/app/api/templates.py", "backend/app/api/signs.py"):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "ZhihuiClient" not in source
        assert "VendorApiError" not in source
        assert "vendor_secret_name" not in source
        assert "vendor_secret_key" not in source

    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    api_block = compose.split("  api:\n", maxsplit=1)[1].split(
        "\n  worker-realtime:\n", maxsplit=1
    )[0]
    assert "vendor_secret_name" not in api_block
    assert "vendor_secret_key" not in api_block


def _schema_block(source: str, schema_name: str) -> str:
    marker = f"    {schema_name}:\n"
    block = source.split(marker, maxsplit=1)[1]
    next_schema = re.search(r"^    [A-Za-z][A-Za-z0-9]+:\n", block, re.MULTILINE)
    return block[: next_schema.start()] if next_schema else block


def test_openapi_console_contract_never_accepts_plaintext_credentials_or_uat_phone() -> None:
    contract = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    credential = _schema_block(contract, "CredentialEnvelopeModel")
    uat = _schema_block(contract, "UatMessageRequestModel")
    operation = _schema_block(contract, "VendorTestOperationModel")

    for forbidden in ("secret_name", "secret_key", "secretName", "secretKey"):
        assert forbidden not in credential
    for forbidden in ("phone:", "mobile:", "phone_enc", "phone_hmac"):
        assert forbidden not in uat
    for forbidden in ("phone", "mobile", "content", "secret", "payload"):
        assert forbidden not in operation.lower()


def test_live_mode_blocks_normal_send_paths_and_vendor_http_stays_in_adapter() -> None:
    messages = (ROOT / "backend/app/api/messages.py").read_text(encoding="utf-8")
    web_messages = (ROOT / "backend/app/api/web_messages.py").read_text(
        encoding="utf-8"
    )
    assert "VENDOR_TEST_CONSOLE_ONLY" in messages
    assert "vendor_test_console_only=settings.vendor_live_test" in messages
    assert "vendor_test_console_only=settings.vendor_live_test" in web_messages

    for relative in (
        "backend/app/api/vendor_test.py",
        "backend/app/services/vendor_control_client.py",
        "backend/app/services/vendor_test_uat.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for forbidden in ("import httpx", "from httpx", "import requests", "aiohttp"):
            assert forbidden not in source


def test_agent_protocol_has_only_fixed_operations_and_no_process_control_fields() -> None:
    protocol = (ROOT / "deploy/scripts/vendor_control_protocol.py").read_text(
        encoding="utf-8"
    )

    assert 'REQUEST_FIELDS = {"schema_version", "operation_id", "operation", "body"}' in protocol
    assert "if fields != _CREDENTIAL_FIELDS" in protocol
    assert 'if fields != {"pause_kind"}' in protocol
    for forbidden in ('"path"', '"argv"', '"env"', "shell=True"):
        assert forbidden not in protocol


def test_control_operation_and_audit_schemas_store_only_safe_metadata() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE vendor_test_operation", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]
    repository = (
        ROOT / "backend/app/services/vendor_test_operation_repository.py"
    ).read_text(encoding="utf-8")
    audit = repository.split("    async def _audit(", maxsplit=1)[1]

    for forbidden in ("phone", "mobile", "content", "secret", "payload", "request_body"):
        assert forbidden not in table.lower()
    for forbidden in (
        'payload["phone',
        'payload["mobile',
        'payload["content',
        'payload["secret',
        'payload["request',
    ):
        assert forbidden not in audit.lower()
