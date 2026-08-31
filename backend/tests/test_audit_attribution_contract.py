from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_schema_rejects_new_legacy_audit_and_uses_transaction_correlation() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    assert "trg_audit_require_live_principal" in schema
    assert "enforce_live_audit_principal" in schema
    assert "current_setting('sms.correlation_id', TRUE)" in schema
    assert "live audit event has no authenticated actor context" in schema
    assert "audit subject does not match authenticated context" in schema
    assert "system audit producer/action is not authorized for database role" in schema
    assert "audit context signature is invalid" in schema
    assert "system audit context signature is invalid" in schema
    assert "system audit event has no authenticated producer context" in schema
    assert "audit_context_signing_key" in schema
    assert "'principal','system:api','system:realtime','system:bulk'" in schema
    assert "WHERE key_kind='principal'" in schema
    assert "WHERE key_kind='system:' || context_domain" in schema
    assert "sms.audit_producer_domain" in schema
    assert "'system-v2'" in schema
    assert "encode(convert_to(context_actor,'UTF8'),'hex')" in schema
    assert "session_user='sms_auth'" in schema
    assert "session_user='sms_accept'" in schema
    assert "session_user='sms_send'" in schema
    assert "NEW.actor='system-reconcile' AND NEW.action='raw_replay'" in schema


def test_usage_projection_reconcile_actor_is_authorized_for_send_workers() -> None:
    from app.services.usage_ledger import RECONCILE_REBUILD_ACTOR

    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    start = schema.index("CREATE OR REPLACE FUNCTION enforce_live_audit_principal()")
    end = schema.index("REVOKE ALL ON FUNCTION enforce_live_audit_principal()")
    function = schema[start:end]
    realtime = function.split("context_domain='realtime'", maxsplit=1)[1].split(
        "context_domain='bulk'",
        maxsplit=1,
    )[0]
    bulk = function.split("context_domain='bulk'", maxsplit=1)[1]
    for domain_sql in (realtime, bulk):
        assert RECONCILE_REBUILD_ACTOR in domain_sql
        assert "usage_projection_rebuild" in domain_sql


def test_runtime_binds_human_and_api_key_principals_before_business_writes() -> None:
    auth_runtime = (ROOT / "backend/app/core/auth/runtime.py").read_text(encoding="utf-8")
    api_key = (ROOT / "backend/app/core/apikey.py").read_text(encoding="utf-8")
    resources = (ROOT / "backend/app/core/runtime_resources.py").read_text(encoding="utf-8")

    assert "bind_audit_principal(claims.principal)" in auth_runtime
    assert "ApplicationPrincipal(context.app_id, context.name, context.dept)" in api_key
    for key in (
        "sms.correlation_id",
        "sms.audit_subject_kind",
        "sms.audit_actor_name",
        "sms.audit_account_id",
        "sms.audit_identity_id",
        "sms.audit_app_id",
        "sms.audit_producer_domain",
        "sms.audit_action",
        "sms.audit_context_signature",
    ):
        assert key in resources


def test_compose_isolates_audit_signers_by_producer_domain() -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    for service in (
        "worker-realtime",
        "worker-bulk",
        "worker-callback",
        "beat",
        "outbox-dispatcher",
    ):
        targets = {item["target"] for item in compose["services"][service]["secrets"]}
        assert "audit_context_key" not in targets
    assert "audit_system_api_context_key" in {
        item["target"] for item in compose["services"]["api"]["secrets"]
    }
    assert "audit_system_realtime_context_key" in {
        item["target"] for item in compose["services"]["worker-realtime"]["secrets"]
    }
    assert "audit_system_bulk_context_key" in {
        item["target"] for item in compose["services"]["worker-bulk"]["secrets"]
    }
    for service in ("worker-callback", "beat", "outbox-dispatcher"):
        targets = {item["target"] for item in compose["services"][service]["secrets"]}
        assert not any(name.startswith("audit_system_") for name in targets)
