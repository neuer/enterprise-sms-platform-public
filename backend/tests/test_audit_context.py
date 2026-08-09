from __future__ import annotations

from app.core.audit_context import (
    canonical_audit_context,
    canonical_system_audit_context,
    sign_audit_context,
    sign_system_audit_context,
)


def test_audit_context_signature_binds_transaction_role_and_principal() -> None:
    values = {
        "txid": 42,
        "database_user": "sms_accept",
        "correlation_id": "30000000-0000-4000-8000-000000000009",
        "subject_kind": "human",
        "actor_name": "admin|测试",
        "account_id": "1",
        "identity_id": "10",
        "app_id": "",
    }
    signature = sign_audit_context(b"k" * 32, **values)

    assert len(signature) == 64
    assert signature != sign_audit_context(
        b"k" * 32, **{**values, "database_user": "sms_send"}
    )
    assert signature != sign_audit_context(b"k" * 32, **{**values, "txid": 43})
    assert signature != sign_audit_context(
        b"k" * 32, **{**values, "actor_name": "other"}
    )
    assert canonical_audit_context(**values).startswith(b"v2\n42\n")


def test_audit_context_uses_unwrapped_hex_for_long_multibyte_actor() -> None:
    payload = canonical_audit_context(
        txid=42,
        database_user="sms_accept",
        correlation_id="30000000-0000-4000-8000-000000000009",
        subject_kind="human",
        actor_name="中" * 64,
        account_id="1",
        identity_id="10",
        app_id="",
    )

    assert payload.count(b"\n") == 8
    assert ("中" * 64).encode().hex().encode() in payload


def test_system_signature_binds_producer_and_action_separately() -> None:
    values = {
        "txid": 42,
        "database_user": "sms_send",
        "correlation_id": "30000000-0000-4000-8000-000000000009",
        "producer_domain": "realtime",
        "actor_name": "vendor-state-sync",
        "action": "template_sync",
    }
    signature = sign_system_audit_context(b"s" * 32, **values)

    assert canonical_system_audit_context(**values).startswith(b"system-v2\n42\n")
    assert signature != sign_system_audit_context(
        b"s" * 32, **{**values, "action": "sign_sync"}
    )
    assert signature != sign_system_audit_context(b"k" * 32, **values)
    assert signature != sign_system_audit_context(
        b"s" * 32, **{**values, "producer_domain": "bulk"}
    )
