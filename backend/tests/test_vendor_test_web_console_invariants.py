from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from classify_ci_changes import classify_paths  # noqa: E402
from test_update_contract import classify_changed_paths  # noqa: E402

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


@pytest.mark.parametrize("path", CONSOLE_HIGH_RISK_PATHS)
def test_console_security_changes_are_excluded_from_quick_update(path: str) -> None:
    scope = classify_changed_paths([path])

    assert scope.risk == "high-risk"
    assert scope.high_risk_paths == (path,)


def test_secure_access_contract_is_short_lived_static_and_has_no_plaintext_fallback() -> None:
    contract = (
        ROOT / "deploy/scripts/test_secure_access_contract.py"
    ).read_text(encoding="utf-8")
    runtime = (ROOT / "deploy/scripts/test_secure_access_runtime.py").read_text(
        encoding="utf-8"
    )
    manager = (ROOT / "deploy/scripts/test_secure_access_manager.py").read_text(
        encoding="utf-8"
    )
    unit = (
        ROOT / "deploy/systemd/sms-platform-test-secure-access.service"
    ).read_text(encoding="utf-8")
    wrapper = (ROOT / "deploy/sms-compose").read_text(encoding="utf-8")
    dialog = (
        ROOT / "frontend/src/components/VendorCredentialDialog.vue"
    ).read_text(encoding="utf-8")
    seal = (ROOT / "frontend/src/lib/vendorSeal.ts").read_text(encoding="utf-8")

    for token in (
        'CLOUDFLARED_VERSION = "2026.7.2"',
        "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd",
        'ORIGIN = "http://127.0.0.1:18080"',
        "MAX_LIFETIME_SECONDS = 900",
    ):
        assert token in contract
    assert '"--protocol",' in runtime and '"http2",' in runtime
    assert '"--no-autoupdate",' in runtime
    assert '{"start", "status", "stop", "verify-assets"}' in manager
    assert 'action == "verify-assets"' in manager
    assert "SMS_SECURE_ACCESS_INTERNAL" in manager
    assert "RuntimeMaxSec=15min" in unit
    assert "[Install]" not in unit and "WantedBy=" not in unit
    assert "dispatch_secure_access()" in wrapper
    assert "prepare_runtime_secrets" not in wrapper.split(
        "dispatch_secure_access()", maxsplit=1
    )[1].split("run_locked_operation()", maxsplit=1)[0]
    assert "isVendorCredentialSecureContext" in dialog
    assert "globalThis.isSecureContext === true" in seal
    assert "globalThis.crypto.subtle" in seal
    for forbidden in (
        "明文提交",
        "localStorage.setItem",
        "sessionStorage.setItem",
    ):
        assert forbidden not in dialog
        assert forbidden not in seal


def test_repository_invariant_gate_covers_every_secure_access_boundary() -> None:
    invariants = (ROOT / "scripts/check_invariants.py").read_text(encoding="utf-8")

    for token in (
        "deploy/scripts/test_secure_access_contract.py",
        "deploy/scripts/test_secure_access_runtime.py",
        "deploy/scripts/test_secure_access_manager.py",
        "deploy/scripts/install_test_secure_access.py",
        "deploy/systemd/sms-platform-test-secure-access.service",
        "frontend/src/components/VendorCredentialDialog.vue",
        "frontend/src/lib/vendorSeal.ts",
        "RuntimeMaxSec=15min",
        "MAX_LIFETIME_SECONDS = 900",
        "globalThis.isSecureContext === true",
        "globalThis.crypto.subtle",
        "dispatch_secure_access()",
    ):
        assert token in invariants


def test_ci_vendor_gate_is_mock_only_and_installs_locked_frontend_runtime() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    backend_job = workflow.split("  backend:\n", maxsplit=1)[1].split(
        "\n  frontend:\n", maxsplit=1
    )[0]

    assert 'node-version: "24"' in backend_job
    assert "npm ci" in backend_job
    assert backend_job.index("npm ci") < backend_job.index(
        "bash scripts/verify_vendor_live_test.sh"
    )
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
        "deploy/scripts/vendor_control_agent.py",
        "deploy/scripts/vendor_control_reload.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for forbidden in ("import httpx", "from httpx", "import requests", "aiohttp"):
            assert forbidden not in source


def test_agent_protocol_has_only_fixed_operations_and_no_process_control_fields() -> None:
    protocol = (ROOT / "deploy/scripts/vendor_control_protocol.py").read_text(
        encoding="utf-8"
    )
    agent = (ROOT / "deploy/scripts/vendor_control_agent.py").read_text(
        encoding="utf-8"
    )

    assert 'REQUEST_FIELDS = {"schema_version", "operation_id", "operation", "body"}' in protocol
    assert "if fields != _CREDENTIAL_FIELDS" in protocol
    assert 'if fields != {"pause_kind"}' in protocol
    assert '[WRAPPER, "vendor-test", operation]' in agent
    for forbidden in ('"path"', '"argv"', '"env"', "shell=True"):
        assert forbidden not in protocol
        assert forbidden not in agent


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
