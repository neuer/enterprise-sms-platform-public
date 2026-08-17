from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_vendor_live_static_invariants_pass_for_repository() -> None:
    result = subprocess.run(
        ["python3", "scripts/check_invariants.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "真实厂商受控联调" in result.stdout


def test_static_gate_covers_all_live_test_safety_boundaries() -> None:
    source = (ROOT / "scripts/check_invariants.py").read_text(encoding="utf-8")

    for token in (
        "vendor_test_console_only=settings.vendor_live_test",
        "require_allowed(request.mobiles)",
        "_guard_chunk(chunk)",
        "LIVE_TEST_DAILY_SEGMENT_LIMIT",
        "DAILY_SEGMENT_LIMIT",
        "pause_queues=True",
        'alert_level="crit"',
        "mock-vendor",
        "_HIGH_RISK_EXACT",
        "reconcile_pure_mock_dotenv(",
        "verify_release.sh",
        "GetReport",
            "GetReply",
            "class VendorTestRotationManager:",
            "self.credentials.rollback_to_previous(transaction)",
            "self.credentials.complete_rollback(rolling_back)",
            "queue:paused:vendor-test-rotation-failed:",
        "protected_mobiles=(",
        "await self.config_store.load_config(app.dept)",
        "previewVendorTestUat",
        "backend/app/tasks/reconcile.py",
        '"expires_at": expires_at.astimezone(UTC).isoformat()',
    ):
        assert token in source


def test_static_gate_forbids_api_phone_decrypt_and_short_wrapper_timeout() -> None:
    source = (ROOT / "scripts/check_invariants.py").read_text(encoding="utf-8")

    assert 'if "decrypt_text(" in uat_service' in source
    assert 'if "timeout=180" not in fixed_runner' in source


def test_static_gate_uses_database_recipients_and_control_state_not_host_allowlist() -> None:
    source = (ROOT / "scripts/check_invariants.py").read_text(encoding="utf-8")

    for forbidden in (
        "VENDOR_TEST_ALLOWLIST_UID",
        "VENDOR_TEST_ALLOWLIST_GID",
        "allowlist_info",
        "vendor live test allowlist must be readable",
    ):
        assert forbidden not in source
    for required in (
        "active_recipient_count < 1",
        "enforce_live_test_recipients=settings.vendor_live_test",
        "vendor_test_recipient",
    ):
        assert required in source


def test_control_plane_schemas_have_no_plain_recipient_content_or_secret_fields() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    ledger = schema.split("CREATE TABLE vendor_test_daily_usage", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]
    attempts = schema.split("CREATE TABLE vendor_test_send_attempt", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]
    evidence_source = (ROOT / "deploy/scripts/vendor_test_files.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("phone_enc", "phone_mask", "content", "secret"):
        assert forbidden not in ledger.lower()
        assert forbidden not in attempts.lower()
    assert "phone_hmac" not in ledger.lower()
    assert "phone_hmac" not in attempts.lower()
    assert "_FORBIDDEN_EVIDENCE_KEY" in evidence_source
    assert "phone|mobile|hmac|secret|password|credential|content|digest|hash|token" in (
        evidence_source
    )


def test_page_only_vendor_test_contract_keeps_plaintext_outside_volatile_browser_memory() -> None:
    contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "浏览器易失内存" in contract
    assert "WebCrypto" in contract
    assert "禁止写入 Pinia、localStorage、sessionStorage、IndexedDB" in contract
    assert "禁止进入普通 API 明文" in contract
    assert "vendor-control-agent" in contract
