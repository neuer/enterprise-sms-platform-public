from __future__ import annotations

import hashlib
import json

import pytest

import scripts_support.recovery_crypto_probe as probe_module
from app.services.crypto import CryptoService, EncryptionContext
from scripts_support.recovery_crypto_probe import (
    EXPECTED_ENCRYPTED_COLUMNS,
    ProbeCounts,
    ProbeCoverage,
    ProbeResult,
    RecoveryCryptoProbeError,
    _verify_audit_keys,
    _verify_encrypted_column_inventory,
    _verify_packed_text_samples,
    _verify_phone_samples,
    _verify_raw_payload_samples,
    _verify_version_inventory,
)


def _crypto(active_version: int) -> CryptoService:
    return CryptoService(
        aes_keys={1: b"a" * 32, 2: b"b" * 32},
        hmac_keys={1: b"c" * 32, 2: b"d" * 32},
        active_version=active_version,
    )


def test_audit_key_binding_requires_exact_constant_time_matches() -> None:
    expected = {
        "principal": b"a" * 32,
        "system:api": b"b" * 32,
        "system:realtime": b"c" * 32,
        "system:bulk": b"d" * 32,
    }

    assert _verify_audit_keys(list(expected.items()), expected) == 4

    mismatched = list(expected.items())
    mismatched[-1] = ("system:bulk", b"z" * 32)
    with pytest.raises(RecoveryCryptoProbeError, match="binding failed"):
        _verify_audit_keys(mismatched, expected)


def test_all_restored_key_version_references_must_exist() -> None:
    assert _verify_version_inventory([[1], [], [1, 2]], (1, 2)) == (3, 2)

    with pytest.raises(RecoveryCryptoProbeError, match="unavailable"):
        _verify_version_inventory([[1, 3]], (1, 2))


def test_each_phone_table_key_version_is_really_decrypted_and_hmac_checked() -> None:
    first = _crypto(1).protect_phone("13800138000")
    second = _crypto(2).protect_phone("13900139000")
    rows = [
        (first.key_version, first.phone_enc, first.phone_hmac),
        (second.key_version, second.phone_enc, second.phone_hmac),
    ]

    assert _verify_phone_samples(rows, _crypto(2), table="sms_message") == 2

    tampered = [(first.key_version, first.phone_enc, "0" * 64)]
    with pytest.raises(RecoveryCryptoProbeError, match="validation failed"):
        _verify_phone_samples(tampered, _crypto(2), table="sms_message")


def test_encrypted_column_inventory_is_an_exact_closed_set() -> None:
    rows = [tuple(label.split(".", 1)) for label in EXPECTED_ENCRYPTED_COLUMNS]
    assert _verify_encrypted_column_inventory(rows) == len(
        EXPECTED_ENCRYPTED_COLUMNS
    )

    with pytest.raises(RecoveryCryptoProbeError, match="inventory"):
        _verify_encrypted_column_inventory(rows[:-1])
    with pytest.raises(RecoveryCryptoProbeError, match="inventory"):
        _verify_encrypted_column_inventory([*rows, ("new_table", "secret_enc")])


def test_bound_text_and_raw_payload_samples_are_really_authenticated() -> None:
    crypto = _crypto(2)
    context = EncryptionContext(
        domain="sms-content",
        table="sms_batch",
        column="send_content_enc",
        object_id="batch-1",
    )
    packed = crypto.encrypt_bound_packed_text("message", context)
    assert (
        _verify_packed_text_samples(
            [(2, packed, "batch-1")],
            crypto,
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
        )
        == 1
    )

    plaintext = b'{"code":0}'
    digest = hashlib.sha256(plaintext).hexdigest()
    raw_context = EncryptionContext(
        domain="vendor-raw",
        table="raw_vendor_log",
        column="payload_enc",
        object_id=f"report:{digest}",
    )
    encrypted = crypto.encrypt_bound_bytes(plaintext, raw_context)
    assert (
        _verify_raw_payload_samples(
            [(2, encrypted.payload, f"report:{digest}", digest)], crypto
        )
        == 1
    )

    with pytest.raises(RecoveryCryptoProbeError, match="validation failed"):
        _verify_raw_payload_samples(
            [(2, encrypted.payload, f"report:{digest}", "0" * 64)], crypto
        )


def test_probe_stdout_is_fixed_json_without_sensitive_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    coverage = {
        label: ProbeCoverage(
            rows=10 if label == "sms_message.phone_enc" else 0,
            key_versions_verified=(
                2 if label == "sms_message.phone_enc" else 0
            ),
        )
        for label in EXPECTED_ENCRYPTED_COLUMNS
    }

    async def fake_probe() -> ProbeResult:
        return ProbeResult(
            ProbeCounts(
                audit_context_keys=4,
                encrypted_columns=len(EXPECTED_ENCRYPTED_COLUMNS),
                encrypted_rows=10,
                ciphertext_samples_verified=2,
                key_version_columns=12,
                referenced_key_versions=2,
                sms_message_rows=10,
            ),
            coverage,
        )

    monkeypatch.setattr(probe_module, "probe", fake_probe)

    assert probe_module.main() == 0
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value == {
        "schema_version": 2,
        "status": "performed",
        "counts": {
            "audit_context_keys": 4,
            "encrypted_columns": len(EXPECTED_ENCRYPTED_COLUMNS),
            "encrypted_rows": 10,
            "ciphertext_samples_verified": 2,
            "key_version_columns": 12,
            "referenced_key_versions": 2,
            "sms_message_rows": 10,
        },
        "coverage": {
            label: {
                "rows": item.rows,
                "key_versions_verified": item.key_versions_verified,
            }
            for label, item in coverage.items()
        },
    }
    assert output.err == ""
    assert "13800138000" not in output.out
    assert "aaaaaaaa" not in output.out


def test_probe_failure_is_generic_and_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failed_probe() -> ProbeResult:
        raise RuntimeError("secret=do-not-expose 13800138000")

    monkeypatch.setattr(probe_module, "probe", failed_probe)

    assert probe_module.main() == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value["status"] == "failed"
    assert set(value) == {"schema_version", "status", "counts", "coverage"}
    assert set(value["coverage"]) == EXPECTED_ENCRYPTED_COLUMNS
    assert output.err == ""
    assert "do-not-expose" not in output.out
    assert "13800138000" not in output.out
