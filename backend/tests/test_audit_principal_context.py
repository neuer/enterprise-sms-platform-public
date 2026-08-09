from __future__ import annotations

from app.core.auth.accounts import ApplicationPrincipal, SecurityPrincipal
from app.core.auth.principal_context import (
    audit_principal_scope,
    bind_audit_principal,
    current_audit_principal,
)
from app.core.runtime_resources import audit_transaction_settings


def test_audit_principal_scope_binds_and_resets_stable_human_subject() -> None:
    principal = SecurityPrincipal(8, 18, "operator01", "平台部", "operator")

    assert current_audit_principal() is None
    with audit_principal_scope():
        bind_audit_principal(principal)
        assert current_audit_principal() == principal
    assert current_audit_principal() is None


def test_database_transaction_settings_keep_subject_and_correlation_separate() -> None:
    principal = ApplicationPrincipal(7, "uat-client", "平台部")

    with audit_principal_scope(principal):
        values = audit_transaction_settings()

    assert values["subject_kind"] == "api_app"
    assert values["actor_name"] == "app:7"
    assert values["account_id"] == ""
    assert values["identity_id"] == ""
    assert values["app_id"] == "7"
    assert values["correlation_id"]


def test_database_transaction_settings_fail_closed_to_empty_subject_without_auth() -> None:
    with audit_principal_scope():
        values = audit_transaction_settings()

    assert values["subject_kind"] == ""
    assert values["actor_name"] == ""
    assert values["account_id"] == ""
    assert values["identity_id"] == ""
    assert values["app_id"] == ""
