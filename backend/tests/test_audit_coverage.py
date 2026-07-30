from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

from fastapi.routing import APIRoute

from app.api import (
    admin,
    approvals,
    apps,
    auth,
    auth_providers,
    blacklist,
    callbacks,
    messages,
    ops,
    replies,
    reports,
    sensitive_words,
    signs,
    templates,
    users,
    vendor_test,
    web_messages,
)
from app.main import create_app
from app.services.app_repository import SqlAppRepository


def test_every_registered_write_route_declares_an_audit_action() -> None:
    """自动枚举路由；新增写端点未加 @audited 时 CI 必须失败。"""

    missing = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if not getattr(route.endpoint, "__audited_action__", None):
            missing.append(f"{','.join(sorted(route.methods))} {route.path}")
    assert missing == []


def test_required_writes_declare_stable_audit_actions() -> None:
    required: tuple[tuple[Callable[..., object], str], ...] = (
        (auth.login, "login"),
        (auth.refresh, "session_refresh"),
        (auth.change_initial_password, "local_password_change"),
        (auth.change_password, "local_password_change"),
        (auth.logout, "logout"),
        (users.create_local_user, "local_account_create"),
        (users.update_user_role, "role_override"),
        (users.update_user_status, "account_status_change"),
        (users.reset_user_password, "local_password_reset"),
        (users.revoke_user_sessions, "force_logout"),
        (auth_providers.save_provider_draft, "auth_provider_save_draft"),
        (auth_providers.test_provider_draft, "auth_provider_test"),
        (auth_providers.activate_provider, "auth_provider_activate"),
        (auth_providers.disable_provider, "auth_provider_disable"),
        (
            auth_providers.replace_provider_role_mappings,
            "auth_provider_role_mappings_replace",
        ),
        (messages.send_message, "message_send"),
        (messages.send_vendor_test_api_uat, "message_send"),
        (web_messages.import_messages, "message_import"),
        (web_messages.send_web_message, "message_send"),
        (approvals.decide_approval, "approval_decision"),
        (apps.create_app, "app_create"),
        (apps.update_app, "app_update"),
        (apps.disable_app, "app_disable"),
        (apps.rotate_key, "app_rotate_key"),
        (apps.revoke_old_key, "app_revoke_old_key"),
        (apps.rotate_callback_secret, "app_rotate_callback_secret"),
        (blacklist.add_blacklist, "blacklist_add"),
        (blacklist.delete_blacklist, "blacklist_delete"),
        (sensitive_words.add_sensitive_words, "sensitive_word_add"),
        (sensitive_words.delete_sensitive_word, "sensitive_word_delete"),
        (templates.create_template, "template_create"),
        (templates.update_template, "template_update"),
        (templates.delete_template, "template_delete"),
        (templates.sync_template, "template_sync"),
        (signs.create_sign, "sign_create"),
        (signs.update_sign, "sign_update"),
        (signs.delete_sign, "sign_delete"),
        (signs.sync_sign, "sign_sync"),
        (reports.create_export, "export_create"),
        (callbacks.retry_callback, "callback_retry"),
        (replies.blacklist_reply, "reply_optout"),
        (ops.replay_raw, "raw_replay"),
        (ops.trigger_job, "job_trigger"),
        (ops.resume_queue, "queue_resume"),
        (ops.create_unmatched_export, "export_create"),
        (admin.update_configs, "config_update"),
        (vendor_test.step_up, "vendor_test_step_up"),
        (vendor_test.create_seal_session, "vendor_test_seal_session"),
        (vendor_test.install_credentials, "vendor_test_credentials"),
        (vendor_test.add_recipient, "vendor_test_recipient_add"),
        (vendor_test.disable_recipient, "vendor_test_recipient_disable"),
        (
            vendor_test.refresh_recipient_hmac_index,
            "vendor_test_recipient_refresh_index",
        ),
        (vendor_test.activate, "vendor_test_activate"),
        (vendor_test.pause, "vendor_test_pause"),
        (vendor_test.resume, "vendor_test_resume"),
        (vendor_test.send_uat_message, "vendor_test_uat_send"),
    )

    missing = [
        f"{function.__module__}.{function.__name__}: {expected}"
        for function, expected in required
        if getattr(function, "__audited_action__", None) != expected
    ]
    assert missing == []


def test_app_audit_numeric_id_has_explicit_bigint_bind_for_asyncpg() -> None:
    source = inspect.getsource(SqlAppRepository._audit)
    assert "CAST(CAST(:app_id AS bigint) AS text)" in source


def test_all_numeric_audit_object_ids_bind_as_bigint_before_text() -> None:
    services = Path(__file__).resolve().parents[1] / "app/services"
    expected = {
            "approval_repository.py": "CAST(CAST(:id AS bigint) AS text)",
            "operations_query.py": "CAST(CAST(:message_id AS bigint) AS text)",
        "sign_repository.py": "CAST(CAST(:id AS bigint) AS text)",
        "template_repository.py": "CAST(CAST(:id AS bigint) AS text)",
    }
    missing = [
        name
        for name, needle in expected.items()
        if needle not in (services / name).read_text(encoding="utf-8")
    ]
    assert missing == []

    export_source = (services / "export_repository.py").read_text(encoding="utf-8")
    assert "CAST(:public_id AS text)" in export_source
    assert "object_id,after_val" in export_source
