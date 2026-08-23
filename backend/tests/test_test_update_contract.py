from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from test_update_contract import (  # noqa: E402
    HOST_CONTROL_PATHS,
    classify_changed_paths,
    classify_public_cutover_paths,
    classify_rebaseline_paths,
    parse_test_update_request,
    validate_verified_status,
)
from test_update_contract import (  # noqa: E402
    TestUpdateContractError as ContractError,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASE_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
DIGEST = "a" * 64
ARCHIVE_DIGEST = "b" * 64


def _request() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "update_id": "test-20260716T120000Z-0123456789ab",
        "base_commit": BASE_COMMIT,
        "commit": COMMIT,
        "source_ref": "origin/feature/example",
        "environment_mode": "pre-live",
        "components": ["api", "web"],
        "images": {
            "api": {
                "ref": f"sms-platform-test-api:{COMMIT}",
                "id": f"sha256:{DIGEST}",
                "archive_file": "api.tar",
                "archive_sha256": ARCHIVE_DIGEST,
            },
            "web": {
                "ref": f"sms-platform-test-web:{COMMIT}",
                "id": f"sha256:{DIGEST}",
                "archive_file": "web.tar",
                "archive_sha256": ARCHIVE_DIGEST,
            },
        },
        "migration": {
            "from": "0015_account_provider_model",
            "target": "0016_example",
            "compatibility": "expand",
        },
    }


def _parse(payload: dict[str, Any]) -> Any:
    return parse_test_update_request(json.dumps(payload))


class _StringSubclass(str):
    pass


def test_parses_exact_request_into_immutable_values() -> None:
    request = _parse(_request())

    assert request.update_id == "test-20260716T120000Z-0123456789ab"
    assert request.base_commit == BASE_COMMIT
    assert request.commit == COMMIT
    assert request.source_ref == "origin/feature/example"
    assert request.environment_mode == "pre-live"
    assert request.components == frozenset({"api", "web"})
    assert request.images["api"].archive_file == "api.tar"
    assert request.images["web"].image_id == f"sha256:{DIGEST}"
    assert request.public_cutover is None
    assert request.migration_from == "0015_account_provider_model"
    assert request.migration_target == "0016_example"
    assert request.migration_compatibility == "expand"
    assert request.operation == "apply"

    with pytest.raises(TypeError):
        request.images["api"] = request.images["web"]  # type: ignore[index]


def test_parses_public_cutover_source_pack_binding() -> None:
    payload = _request()
    payload["source_ref"] = "origin/main"
    payload["public_cutover"] = {
        "source_commit": "c" * 40,
        "private_merge_base": "d" * 40,
        "pack_file": "cutover-source.pack",
        "pack_sha256": "e" * 64,
    }

    request = _parse(payload)

    assert request.public_cutover is not None
    assert request.public_cutover.source_commit == "c" * 40
    assert request.public_cutover.private_merge_base == "d" * 40
    assert request.public_cutover.pack_file == "cutover-source.pack"
    assert request.public_cutover.pack_sha256 == "e" * 64


def test_parses_explicit_rebaseline_operation() -> None:
    payload = _request()
    payload["operation"] = "rebaseline"
    payload["source_ref"] = "origin/main"

    assert _parse(payload).operation == "rebaseline"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"operation": "publish"}, "operation"),
        ({"operation": True}, "operation"),
        (
            {"operation": "rebaseline", "source_ref": "origin/feature/example"},
            "origin/main",
        ),
        (
            {
                "operation": "rebaseline",
                "source_ref": "origin/main",
                "components": ["api"],
            },
            "api and web",
        ),
    ],
)
def test_rejects_invalid_rebaseline_request_scope(
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = _request()
    payload.update(mutation)
    if payload["components"] == ["api"]:
        del payload["images"]["web"]

    with pytest.raises(ContractError, match=message):
        _parse(payload)


def test_rebaseline_rejects_cutover_and_requires_expand_migration() -> None:
    payload = _request()
    payload["operation"] = "rebaseline"
    payload["source_ref"] = "origin/main"
    payload["public_cutover"] = {
        "source_commit": "c" * 40,
        "private_merge_base": "d" * 40,
        "pack_file": "cutover-source.pack",
        "pack_sha256": "e" * 64,
    }
    with pytest.raises(ContractError, match="must not carry public_cutover"):
        _parse(payload)

    del payload["public_cutover"]
    payload["migration"] = {
        "from": "0015_account_provider_model",
        "target": "0015_account_provider_model",
        "compatibility": "none",
    }
    with pytest.raises(ContractError, match="requires expand migration"):
        _parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", "c" * 39),
        ("private_merge_base", "D" * 40),
        ("pack_file", "../cutover-source.pack"),
        ("pack_file", "other.pack"),
        ("pack_sha256", "e" * 63),
    ],
)
def test_rejects_unsafe_public_cutover_binding(
    field: str,
    value: object,
) -> None:
    payload = _request()
    payload["source_ref"] = "origin/main"
    payload["public_cutover"] = {
        "source_commit": "c" * 40,
        "private_merge_base": "d" * 40,
        "pack_file": "cutover-source.pack",
        "pack_sha256": "e" * 64,
    }
    payload["public_cutover"][field] = value

    with pytest.raises(ContractError, match="public_cutover"):
        _parse(payload)


def test_rejects_public_cutover_binding_on_non_main_ref() -> None:
    payload = _request()
    payload["public_cutover"] = {
        "source_commit": "c" * 40,
        "private_merge_base": "d" * 40,
        "pack_file": "cutover-source.pack",
        "pack_sha256": "e" * 64,
    }

    with pytest.raises(ContractError, match="origin/main"):
        _parse(payload)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        json.dumps(_request()).encode(),
        bytearray(json.dumps(_request()).encode()),
        None,
        _StringSubclass(json.dumps(_request())),
    ],
)
def test_rejects_request_inputs_that_are_not_exact_str(raw: object) -> None:
    with pytest.raises(ContractError, match="exact str"):
        parse_test_update_request(raw)  # type: ignore[arg-type]


def test_rejects_duplicate_json_keys() -> None:
    raw = json.dumps(_request())
    duplicate = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')

    with pytest.raises(ContractError, match="duplicate JSON key"):
        parse_test_update_request(duplicate)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            f'"ref": "sms-platform-test-api:{COMMIT}"',
            (f'"ref": "sms-platform-test-api:{COMMIT}", "ref": "sms-platform-test-api:{COMMIT}"'),
        ),
        (
            '"compatibility": "expand"',
            '"compatibility": "expand", "compatibility": "expand"',
        ),
    ],
)
def test_rejects_duplicate_json_keys_at_any_nested_level(
    needle: str,
    replacement: str,
) -> None:
    nested_duplicate = json.dumps(_request()).replace(needle, replacement, 1)

    with pytest.raises(ContractError, match="duplicate JSON key"):
        parse_test_update_request(nested_duplicate)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ((), "unexpected"),
        (("images", "api"), "platform"),
        (("migration",), "downgrade"),
    ],
)
def test_rejects_unknown_fields(location: tuple[str, ...], field: str) -> None:
    payload = _request()
    target = payload
    for part in location:
        target = target[part]
    target[field] = "not-allowed"

    with pytest.raises(ContractError, match="unknown fields"):
        _parse(payload)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ((), "source_ref"),
        (("images", "api"), "archive_sha256"),
        (("migration",), "target"),
    ],
)
def test_rejects_missing_fields(location: tuple[str, ...], field: str) -> None:
    payload = _request()
    target = payload
    for part in location:
        target = target[part]
    del target[field]

    with pytest.raises(ContractError, match="missing fields"):
        _parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("update_id", "../test-20260716T120000Z-0123456789ab"),
        ("update_id", "test-20260716T120000Z-0123456789AB"),
        ("base_commit", "a" * 39),
        ("base_commit", "A" * 40),
        ("commit", "a" * 41),
        ("source_ref", "feature/example"),
        ("source_ref", "origin/../main"),
        ("source_ref", "origin/foo.lock/bar"),
        ("source_ref", "origin/feature example"),
        ("environment_mode", "unknown"),
    ],
)
def test_rejects_unsafe_top_level_values(field: str, value: object) -> None:
    payload = _request()
    payload[field] = value

    with pytest.raises(ContractError, match="invalid"):
        _parse(payload)


@pytest.mark.parametrize("components", [[], ["postgres"], ["api", "api"], "api"])
def test_rejects_invalid_component_subsets(components: object) -> None:
    payload = _request()
    payload["components"] = components

    with pytest.raises(ContractError, match="components"):
        _parse(payload)


def test_accepts_each_single_component_with_its_bound_image() -> None:
    for component in ("api", "web"):
        payload = _request()
        payload["components"] = [component]
        payload["images"] = {component: deepcopy(payload["images"][component])}

        request = _parse(payload)

        assert request.components == frozenset({component})
        assert tuple(request.images) == (component,)


def test_requires_images_to_match_selected_components_exactly() -> None:
    payload = _request()
    payload["components"] = ["api"]

    with pytest.raises(ContractError, match="selected components"):
        _parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ref", f"sms-platform-test-web:{COMMIT}"),
        ("ref", "sms-platform-test-api:latest"),
        ("id", "sha256:" + "a" * 63),
        ("archive_file", "../api.tar"),
        ("archive_file", "/tmp/api.tar"),
        ("archive_file", "web.tar"),
        ("archive_sha256", "b" * 63),
    ],
)
def test_rejects_unbound_or_unsafe_image_values(field: str, value: str) -> None:
    payload = _request()
    payload["images"]["api"][field] = value

    with pytest.raises(ContractError, match="image api"):
        _parse(payload)


@pytest.mark.parametrize("compatibility", ["manual", "contract", "NONE", None])
def test_rejects_unknown_migration_compatibility(compatibility: object) -> None:
    payload = _request()
    payload["migration"]["compatibility"] = compatibility

    with pytest.raises(ContractError, match="migration compatibility"):
        _parse(payload)


def test_none_migration_requires_equal_heads() -> None:
    payload = _request()
    payload["migration"] = {
        "from": "0015_account_provider_model",
        "target": "0015_account_provider_model",
        "compatibility": "none",
    }
    assert _parse(payload).migration_compatibility == "none"

    payload["migration"]["target"] = "0016_example"
    with pytest.raises(ContractError, match="none requires equal"):
        _parse(payload)


def test_expand_migration_requires_different_heads() -> None:
    payload = _request()
    payload["migration"]["target"] = payload["migration"]["from"]

    with pytest.raises(ContractError, match="expand requires different"):
        _parse(payload)


@pytest.mark.parametrize("head", ["../0015", "0015/head", "0015 head", ""])
def test_rejects_unsafe_migration_head_names(head: str) -> None:
    payload = _request()
    payload["migration"]["target"] = head

    with pytest.raises(ContractError, match="migration target"):
        _parse(payload)


def test_classifies_application_paths_without_expanding_to_data_services() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/auth.py",
            "backend/tests/test_auth_api.py",
            "frontend/src/views/LoginView.vue",
            "frontend/tests/login-view.test.ts",
            "backend/migrations/versions/0016_example.py",
            "schema.sql",
        ]
    )

    assert change.components == frozenset({"api", "web"})
    assert change.migration_changed is True
    assert change.backend_tests == ("backend/tests/test_auth_api.py",)
    assert change.frontend_tests == ("frontend/tests/login-view.test.ts",)
    assert change.runtime_changed is True
    assert change.risk == "high-risk"


@pytest.mark.parametrize(
    ("path", "expected_risk"),
    [
        ("backend/migrations/versions/0016_example.py", "backend-safe"),
        ("schema.sql", "high-risk"),
    ],
)
def test_each_migration_source_sets_migration_changed(
    path: str,
    expected_risk: str,
) -> None:
    change = classify_changed_paths([path])

    assert change.components == frozenset({"api"})
    assert change.migration_changed is True
    assert change.runtime_changed is True
    assert change.risk == expected_risk


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/api/messages.py",
        "backend/app/api/web_messages.py",
        "backend/app/cli.py",
        "backend/app/settings.py",
        "backend/app/services/pipeline.py",
        "backend/app/services/reconcile_repository.py",
        "backend/app/vendor/zhihui.py",
        "backend/app/vendor/codes.py",
        "backend/app/tasks/reconcile.py",
        "backend/app/tasks/send.py",
        "backend/app/services/billing.py",
        "backend/app/services/vendor_test_guard.py",
        "backend/app/services/vendor_test_budget.py",
        "backend/app/tasks/send_repository.py",
        "backend/migrations/versions/0016_vendor_live_test_budget.py",
        "backend/migrations/versions/0017_vendor_test_web_console.py",
        "backend/migrations/versions/0018_vendor_test_operation_vendor_code.py",
        "backend/migrations/versions/0019_vendor_test_recipient_hmac_alias.py",
        "backend/migrations/versions/0022_vendor_test_reset_operation.py",
        "backend/migrations/versions/0023_vendor_uat_acceptance_lease.py",
        "backend/app/api/vendor_test.py",
        "backend/app/core/auth/runtime.py",
        "backend/app/services/vendor_control_client.py",
        "backend/app/services/vendor_test_operation.py",
        "backend/app/services/vendor_test_recipient.py",
        "backend/app/services/vendor_test_security_audit.py",
        "backend/app/services/vendor_test_step_up.py",
        "backend/app/services/vendor_test_uat.py",
        "backend/app/services/vendor_test_pause.py",
        "backend/vendor_control_protocol.py",
        "backend/vendor_control_protocol.pyi",
        "backend/Dockerfile",
        "deploy/scripts/vendor_test_manager.py",
        "deploy/scripts/vendor_test_bootstrap.py",
        "deploy/scripts/vendor_test_files.py",
        "deploy/scripts/vendor_control_agent.py",
        "deploy/scripts/vendor_control_reload.py",
        "deploy/scripts/vendor_control_protocol.py",
        "deploy/scripts/vendor_credential_store.py",
        "deploy/scripts/vendor_seal_sessions.py",
        "deploy/systemd/vendor-control-agent.service",
        "deploy/scripts/install_test_secure_access.py",
        "deploy/scripts/test_secure_access_contract.py",
        "deploy/scripts/test_secure_access_manager.py",
        "deploy/scripts/test_secure_access_runtime.py",
        "deploy/systemd/sms-platform-test-secure-access.service",
        "deploy/sms-compose",
        "scripts/check_invariants.py",
        "scripts/classify_ci_changes.py",
        "scripts/test_update.sh",
        "frontend/src/api/admin.ts",
        "frontend/src/components/VendorCredentialDialog.vue",
        "frontend/src/components/VendorTestConsole.vue",
        "frontend/src/lib/vendorSeal.ts",
        "frontend/src/views/ConfigView.vue",
        "openapi.yaml",
        "schema.sql",
        "deploy/scripts/test_update_manager.py",
        "deploy/docker-compose.yml",
        "deploy/.env.example",
        ".dockerignore",
        "deploy/scripts/install_resend_api_key.py",
        "deploy/scripts/send_security_daily_report_resend.py",
        "deploy/security-report/Dockerfile",
        "deploy/security-report/docker-compose.yml",
        "deploy/templates/security_daily_report.html",
        "deploy/templates/security_daily_report.txt",
        "deploy/scripts/render_security_daily_report.py",
    ],
)
def test_classifies_vendor_live_control_changes_as_high_risk(path: str) -> None:
    change = classify_changed_paths([path])

    assert change.risk == "high-risk"
    assert change.high_risk_paths == (path,)


@pytest.mark.parametrize(
    ("path", "components"),
    [
        ("backend/app/api/auth.py", frozenset({"api"})),
        ("backend/app/core/auth/jwt.py", frozenset({"api"})),
        ("backend/app/services/crypto.py", frozenset({"api"})),
        ("backend/app/services/masking.py", frozenset({"api"})),
        ("backend/app/services/raw_spill.py", frozenset({"api"})),
        ("backend/app/services/usage_ledger.py", frozenset({"api"})),
        ("backend/app/tasks/poll_report.py", frozenset({"api"})),
        ("frontend/src/api/auth.ts", frozenset({"web"})),
        ("frontend/src/api/refreshLock.ts", frozenset({"web"})),
        ("frontend/src/api/sessionTokens.ts", frozenset({"web"})),
        ("frontend/src/api/webMessages.ts", frozenset({"web"})),
        ("frontend/src/stores/session.ts", frozenset({"web"})),
    ],
)
def test_ci_and_test_update_share_backend_critical_paths(
    path: str,
    components: frozenset[str],
) -> None:
    change = classify_changed_paths([path])

    assert change.risk == "high-risk"
    assert change.components == components
    assert change.high_risk_paths == (path,)


@pytest.mark.parametrize(
    "path",
    [
        "deploy/postgres.Dockerfile",
        "deploy/redis.Dockerfile",
        "deploy/initdb/01-create-app-role.sh",
        "deploy/systemd/sms-platform.service",
        "deploy/scripts/release_manager.py",
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/vendor_runtime_reset.py",
        "deploy/unknown-runtime.conf",
    ],
)
def test_rejects_data_or_release_control_changes(path: str) -> None:
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_changed_paths([path])


def test_classifies_nginx_as_web_and_ignores_documentation_runtime() -> None:
    change = classify_changed_paths(
        [
            "deploy/nginx.conf",
            "docs/plans/2026-07-16-test-fast-update.md",
            "docs/TEST-REPORT-FAST-UPDATE.md",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.backend_tests == ()
    assert change.frontend_tests == ()
    assert change.risk == "web-only"


@pytest.mark.parametrize(
    "classifier",
    [classify_changed_paths, classify_public_cutover_paths],
)
def test_classifies_nginx_security_headers_as_high_risk_web(
    classifier: Any,
) -> None:
    change = classifier(["deploy/nginx-security-headers.conf"])

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.high_risk_paths == ("deploy/nginx-security-headers.conf",)


@pytest.mark.parametrize(
    "classifier",
    [classify_changed_paths, classify_public_cutover_paths],
)
def test_classifies_redis_domain_runtime_as_high_risk_api(
    classifier: Any,
) -> None:
    paths = [
        "deploy/redis-domain-entrypoint.sh",
        "deploy/redis-domain-healthcheck.sh",
    ]

    change = classifier(paths)

    assert change.components == frozenset({"api"})
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.high_risk_paths == tuple(sorted(paths))


def test_documentation_and_tests_alone_are_not_runtime_changes() -> None:
    change = classify_changed_paths(
        [
            "docs/plans/design.md",
            "docs/TEST-REPORT-20260716.md",
            "backend/tests/test_auth_api.py",
            "frontend/tests/login-view.test.ts",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.backend_tests == ("backend/tests/test_auth_api.py",)
    assert change.frontend_tests == ("frontend/tests/login-view.test.ts",)
    assert change.risk == "none"


def test_g2_api_acceptance_script_is_explicitly_non_runtime() -> None:
    change = classify_changed_paths(
        [
            "scripts/e2e_api.py",
            "backend/tests/test_e2e_api.py",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.backend_tests == ("backend/tests/test_e2e_api.py",)
    assert change.risk == "none"


def test_security_acceptance_script_is_explicitly_non_runtime() -> None:
    change = classify_changed_paths(["scripts/security_acceptance.py"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"
    assert change.high_risk_paths == ()


def test_database_role_gate_inputs_are_safe_non_runtime_changes() -> None:
    change = classify_changed_paths(
        [
            "deploy/database-roles.md",
            "deploy/secrets.md",
            "docs/runbooks/public-baseline-activation.md",
            "scripts/verify_database_roles.sh",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_security_report_control_placeholder_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths(["deploy/security-report-control/.gitignore"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_root_gitignore_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths([".gitignore"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_local_dev_check_script_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths(["scripts/dev_check.sh"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_local_test_script_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths(["scripts/local_test.sh"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_security_report_ui_config_placeholder_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths(["deploy/security-report-config/.gitignore"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_test_update_config_example_is_safe_non_runtime_input() -> None:
    change = classify_changed_paths(["test-update.env.example"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_runtime_verification_scripts_are_explicitly_non_runtime() -> None:
    change = classify_changed_paths(
        [
            "scripts/verify_redis_domains.sh",
            "scripts/verify_release_control.sh",
            "scripts/verify_tls_termination_e2e.py",
            "scripts/verify_all.sh",
            "scripts/verify_ci_commit.py",
            "scripts/verify_vendor_postgres_recovery.sh",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_secure_access_operational_docs_are_safe_non_runtime_inputs() -> None:
    change = classify_changed_paths(
        [
            "deploy/README.md",
            "deploy/prometheus.example.yml",
            "deploy/redis-ha.md",
            "deploy/vendor-egress.md",
            "docs/DECISIONS.md",
            "docs/LOCAL_TESTING.md",
            "docs/TEST-MANUAL.md",
            "docs/UAT.md",
            "docs/ui-design.md",
            "docs/api-test-playground.md",
            "docs/api-integration.md",
            "docs/runbooks/controlled-real-vendor-test.md",
            "docs/runbooks/test-fast-update.md",
            "docs/previews/approval-redesign-prototype.html",
            "docs/previews/batch-density-prototype.html",
            "docs/previews/batch-redesign-prototype.html",
            "docs/previews/blacklist-redesign-prototype.html",
            "docs/previews/blacklist-redesign-shots.md",
            "docs/previews/blacklist-redesign-shots/full.png",
            "docs/previews/dashboard-redesign-prototype.html",
            "docs/previews/filter-bar-single-row-prototype.html",
            "docs/previews/login-redesign-prototype.html",
            "docs/previews/login-redesign-shots.md",
            "docs/previews/login-redesign-shots/ad.png",
            "docs/previews/login-redesign-shots/choose.png",
            "docs/previews/login-redesign-shots/compare.png",
            "docs/previews/login-redesign-shots/error.png",
            "docs/previews/login-redesign-shots/local.png",
            "docs/previews/login-redesign-shots/only.png",
            "docs/previews/messages-redesign-prototype.html",
            "docs/previews/messages-redesign-shots.md",
            "docs/previews/messages-redesign-shots/full.png",
            "docs/previews/report-redesign-prototype.html",
            "docs/previews/replies-redesign-prototype.html",
            "docs/previews/replies-redesign-shots.md",
            "docs/previews/replies-redesign-shots/full.png",
            "docs/previews/security-daily-report-sample.html",
            "docs/previews/security-daily-report-sample.txt",
            "docs/previews/send-redesign-prototype.html",
            "docs/previews/sensitive-redesign-prototype.html",
            "docs/previews/signs-redesign-prototype.html",
            "docs/previews/signs-redesign-shots.md",
            "docs/previews/signs-redesign-shots/full.png",
            "docs/previews/templates-redesign-prototype.html",
            "docs/previews/templates-redesign-shots.md",
            "docs/previews/templates-redesign-shots/full.png",
            "PROGRESS.md",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_login_redesign_preview_does_not_block_web_only_update() -> None:
    change = classify_changed_paths(
        [
            "docs/previews/login-redesign-prototype.html",
            "docs/previews/login-redesign-shots.md",
            "docs/previews/login-redesign-shots/compare.png",
            "docs/ui-design.md",
            "frontend/src/styles/theme.css",
            "frontend/src/styles/workspace.css",
            "frontend/src/views/LoginView.vue",
            "frontend/src/views/PasswordChangeView.vue",
            "frontend/tests/login-view.test.ts",
            "frontend/tests/password-change-view.test.ts",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "web-only"
    assert change.migration_changed is False


def test_dashboard_redesign_preview_does_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/reports.py",
            "backend/app/services/dashboard.py",
            "docs/previews/dashboard-redesign-prototype.html",
            "frontend/src/views/DashboardView.vue",
            "frontend/tests/dashboard-view.test.ts",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_report_and_send_previews_do_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/reports.py",
            "backend/app/api/web_messages.py",
            "docs/previews/report-redesign-prototype.html",
            "docs/previews/send-redesign-prototype.html",
            "frontend/src/views/ReportView.vue",
            "frontend/src/views/SendView.vue",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_approval_preview_does_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/approvals.py",
            "backend/app/services/approval.py",
            "docs/previews/approval-redesign-prototype.html",
            "frontend/src/views/ApprovalView.vue",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_batch_preview_does_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/web_messages.py",
            "backend/app/services/batch_query.py",
            "docs/previews/batch-redesign-prototype.html",
            "frontend/src/views/BatchView.vue",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_messages_preview_does_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/web_messages.py",
            "docs/previews/messages-redesign-prototype.html",
            "docs/previews/messages-redesign-shots.md",
            "docs/previews/messages-redesign-shots/full.png",
            "frontend/src/views/MessageView.vue",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_replies_preview_does_not_block_classified_update() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/replies.py",
            "backend/app/services/reply_query.py",
            "docs/previews/replies-redesign-prototype.html",
            "docs/previews/replies-redesign-shots.md",
            "docs/previews/replies-redesign-shots/full.png",
            "frontend/src/views/ReplyView.vue",
            "openapi.yaml",
        ]
    )

    assert "web" in change.components
    assert "api" in change.components
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.migration_changed is False


def test_batch_density_preview_does_not_block_web_only_update() -> None:
    change = classify_changed_paths(
        [
            "docs/previews/batch-density-prototype.html",
            "docs/ui-design.md",
            "frontend/src/styles/workspace.css",
            "frontend/src/views/BatchView.vue",
            "frontend/tests/filter-layout-contract.test.ts",
            "frontend/tests/qingluan-screen-fidelity.test.ts",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "web-only"
    assert change.migration_changed is False


def test_templates_preview_does_not_block_web_only_update() -> None:
    change = classify_changed_paths(
        [
            "docs/previews/templates-redesign-prototype.html",
            "docs/previews/templates-redesign-shots.md",
            "docs/previews/templates-redesign-shots/full.png",
            "docs/ui-design.md",
            "frontend/src/styles/workspace.css",
            "frontend/src/views/SendView.vue",
            "frontend/src/views/TemplateView.vue",
            "frontend/tests/send-view.test.ts",
            "frontend/tests/template-view.test.ts",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "web-only"
    assert change.migration_changed is False


def test_signs_preview_does_not_block_web_only_update() -> None:
    change = classify_changed_paths(
        [
            "docs/previews/signs-redesign-prototype.html",
            "docs/previews/signs-redesign-shots.md",
            "docs/previews/signs-redesign-shots/full.png",
            "docs/ui-design.md",
            "frontend/src/styles/workspace.css",
            "frontend/src/views/SignView.vue",
            "frontend/tests/sign-view.test.ts",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "web-only"
    assert change.migration_changed is False


def test_filter_bar_preview_does_not_block_web_only_update() -> None:
    change = classify_changed_paths(
        [
            "docs/previews/filter-bar-single-row-prototype.html",
            "frontend/src/styles/workspace.css",
            "frontend/src/views/ApprovalView.vue",
            "frontend/src/views/BatchView.vue",
            "frontend/tests/approval-view.test.ts",
        ]
    )

    assert change.components == frozenset({"web"})
    assert change.runtime_changed is True
    assert change.risk == "web-only"
    assert change.migration_changed is False


def test_repository_guidance_and_rehearsal_report_are_safe_non_runtime_inputs() -> None:
    change = classify_changed_paths(
        [
            "AGENTS.md",
            "CLAUDE.md",
            "PRD.md",
            "docs/reports/2026-07-18-test-fast-update-rehearsal.md",
        ]
    )

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"


def test_openapi_contract_is_explicitly_non_runtime() -> None:
    change = classify_changed_paths(["openapi.yaml"])

    assert change.components == frozenset()
    assert change.migration_changed is False
    assert change.runtime_changed is False


@pytest.mark.parametrize(
    "path",
    [
        "scripts/future_runtime.py",
        "future/runtime.py",
    ],
)
def test_rejects_every_unclassified_path(path: str) -> None:
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_changed_paths([path])


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/auto-merge-owner-pr.yml",
        ".github/CODEOWNERS",
    ],
)
def test_classifies_github_metadata_as_non_runtime(path: str) -> None:
    change = classify_changed_paths([path])

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"
    assert change.migration_changed is False


def test_github_metadata_does_not_block_mixed_application_change() -> None:
    change = classify_changed_paths(
        [
            "backend/app/api/replies.py",
            ".github/workflows/auto-merge-owner-pr.yml",
            "docs/DECISIONS.md",
        ]
    )

    assert change.components == frozenset({"api"})
    assert change.runtime_changed is True
    assert change.risk == "backend-safe"


def test_docker_compose_change_marks_api_and_web() -> None:
    change = classify_changed_paths(["deploy/docker-compose.yml"])

    assert change.components == frozenset({"api", "web"})
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.high_risk_paths == ("deploy/docker-compose.yml",)


def test_rejects_unclassified_path_mixed_with_known_application_change() -> None:
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_changed_paths(
            [
                "backend/app/main.py",
                "scripts/future_runtime.py",
            ]
        )


def test_rebaseline_accepts_only_the_reviewed_migration_baseline_scope() -> None:
    paths = [
        "README.md",
        "docs/PERFORMANCE.md",
        "scripts/perf_smoke.py",
        "scripts/verify_ci_commit.py",
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/vendor_runtime_reset.py",
        "backend/migrations/versions/0061_vendor_binding_outbox.py",
        "frontend/src/views/SignView.vue",
    ]

    change = classify_rebaseline_paths(paths)

    assert change.components == frozenset({"api", "web"})
    assert change.migration_changed is True
    assert change.runtime_changed is True
    assert change.risk == "high-risk"
    assert change.high_risk_paths == (
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/vendor_runtime_reset.py",
    )


def test_performance_gate_evidence_is_non_runtime_for_daily_apply() -> None:
    change = classify_changed_paths(["docs/PERFORMANCE.md", "scripts/perf_smoke.py"])

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"
    assert change.high_risk_paths == ()


def test_rebaseline_requires_a_migration_change() -> None:
    with pytest.raises(ContractError, match="migration change"):
        classify_rebaseline_paths(
            [
                "deploy/scripts/prepare_runtime_secrets.py",
                "deploy/scripts/vendor_runtime_reset.py",
                "backend/app/main.py",
            ]
        )


def test_rebaseline_requires_the_exact_runtime_control_path_set() -> None:
    with pytest.raises(ContractError, match="exact approved runtime-control"):
        classify_rebaseline_paths(
            [
                "deploy/scripts/prepare_runtime_secrets.py",
                "backend/migrations/versions/0061_vendor_binding_outbox.py",
            ]
        )


def test_rebaseline_still_rejects_unknown_runtime_paths() -> None:
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_rebaseline_paths(
            [
                "deploy/scripts/prepare_runtime_secrets.py",
                "deploy/scripts/vendor_runtime_reset.py",
                "backend/migrations/versions/0061_vendor_binding_outbox.py",
                "deploy/scripts/future.py",
            ]
        )


def test_nul_cli_is_the_single_driver_classification_source() -> None:
    classifier = ROOT / "deploy/scripts/test_update_contract.py"
    result = subprocess.run(
        [sys.executable, str(classifier), "classify-nul"],
        input=(
            b"frontend/src/components/VendorCredentialDialog.vue\0"
            b"deploy/scripts/test_secure_access_manager.py\0"
            b"deploy/systemd/sms-platform-test-secure-access.service\0"
        ),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "components": ["api", "web"],
        "high_risk_paths": [
            "deploy/scripts/test_secure_access_manager.py",
            "deploy/systemd/sms-platform-test-secure-access.service",
            "frontend/src/components/VendorCredentialDialog.vue",
        ],
        "migration_changed": False,
        "risk": "high-risk",
        "runtime_changed": True,
    }


def test_rebaseline_nul_cli_uses_the_strict_rebaseline_classifier() -> None:
    classifier = ROOT / "deploy/scripts/test_update_contract.py"
    result = subprocess.run(
        [sys.executable, str(classifier), "classify-rebaseline-nul"],
        input=(
            b"scripts/perf_smoke.py\0"
            b"scripts/verify_ci_commit.py\0"
            b"deploy/scripts/prepare_runtime_secrets.py\0"
            b"deploy/scripts/vendor_runtime_reset.py\0"
            b"backend/migrations/versions/0061_vendor_binding_outbox.py\0"
            b"frontend/src/views/SignView.vue\0"
        ),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "components": ["api", "web"],
        "high_risk_paths": [
            "deploy/scripts/prepare_runtime_secrets.py",
            "deploy/scripts/vendor_runtime_reset.py",
        ],
        "migration_changed": True,
        "risk": "high-risk",
        "runtime_changed": True,
    }


def test_verified_status_requires_exact_terminal_identity() -> None:
    update_id = "test-20260716T120000Z-0123456789ab"
    raw = json.dumps(
        {
            "update_id": update_id,
            "state": "verified",
            "actual_commit": COMMIT,
            "actual_migration_head": "0019_vendor_test_recipient_hmac_alias",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    validate_verified_status(
        raw,
        expected_update_id=update_id,
        expected_commit=COMMIT,
        expected_migration_head="0019_vendor_test_recipient_hmac_alias",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "blocked"),
        ("actual_commit", BASE_COMMIT),
        ("actual_migration_head", "0018_vendor_test_operation_vendor_code"),
        ("update_id", "test-20260716T120001Z-0123456789ab"),
    ],
)
def test_verified_status_rejects_success_exit_with_wrong_terminal_state(
    field: str,
    value: str,
) -> None:
    update_id = "test-20260716T120000Z-0123456789ab"
    payload = {
        "update_id": update_id,
        "state": "verified",
        "actual_commit": COMMIT,
        "actual_migration_head": "0019_vendor_test_recipient_hmac_alias",
    }
    payload[field] = value

    with pytest.raises(ContractError, match="verified status"):
        validate_verified_status(
            json.dumps(payload),
            expected_update_id=update_id,
            expected_commit=COMMIT,
            expected_migration_head="0019_vendor_test_recipient_hmac_alias",
        )


def test_complete_secure_access_delivery_scope_is_classified_high_risk() -> None:
    change = classify_changed_paths(
        [
            "frontend/src/components/VendorCredentialDialog.vue",
            "frontend/src/lib/vendorSeal.ts",
            "deploy/scripts/install_test_secure_access.py",
            "deploy/scripts/test_secure_access_contract.py",
            "deploy/scripts/test_secure_access_manager.py",
            "deploy/scripts/test_secure_access_runtime.py",
            "deploy/scripts/test_update_manager.py",
            "deploy/systemd/sms-platform-test-secure-access.service",
            "deploy/sms-compose",
            "scripts/check_invariants.py",
            "scripts/classify_ci_changes.py",
            "scripts/test_update.sh",
            "docs/runbooks/controlled-real-vendor-test.md",
            "docs/runbooks/test-fast-update.md",
        ]
    )

    assert change.risk == "high-risk"
    assert change.components == frozenset({"api", "web"})


@pytest.mark.parametrize("path", sorted(HOST_CONTROL_PATHS))
def test_every_host_control_asset_is_high_risk_outside_cutover(path: str) -> None:
    change = classify_changed_paths([path])

    assert change.risk == "high-risk"
    assert change.components == frozenset({"api"})
    assert change.high_risk_paths == (path,)


def test_public_cutover_accepts_the_complete_host_control_asset_set() -> None:
    change = classify_public_cutover_paths(
        [
            *HOST_CONTROL_PATHS,
            "backend/migrations/versions/0028_usage_fact_ledger.py",
        ]
    )

    assert change.risk == "high-risk"
    assert change.components == frozenset({"api"})
    assert change.migration_changed is True
    assert "deploy/scripts/check_test_update_migration.py" in change.high_risk_paths
    assert "deploy/scripts/run_with_lifecycle_lock.py" in change.high_risk_paths


def test_public_cutover_classifies_only_verified_transition_exceptions() -> None:
    change = classify_public_cutover_paths(
        [
            ".github/workflows/ci.yml",
            ".github/workflows/release-gate.yml",
            "HANDOVER.md",
            "PROGRESS.md",
            "README.md",
            "VERSION",
            "deploy/database-roles.md",
            "deploy/runtime-security.md",
            "docs/runbooks/usage-ledger-recovery.md",
            "scripts/check_coverage_gates.py",
            "scripts/release_metadata.py",
            "scripts/verify_ci_results.py",
            "scripts/verify_data_images.sh",
            "scripts/verify_database_roles.sh",
            "scripts/verify_public_snapshot_cutover.py",
            "deploy/initdb/01-create-app-role.sh",
            "deploy/lifecycle.server.example.json",
            "deploy/postgres.Dockerfile",
            "deploy/provision-db-roles.sh",
            "deploy/redis.Dockerfile",
            "deploy/scripts/collect_security_daily_evidence.py",
            "deploy/scripts/lifecycle_manager.py",
            "deploy/scripts/prepare_runtime_secrets.py",
            "deploy/scripts/release_manifest.py",
            "deploy/scripts/release_manager.py",
            "deploy/scripts/restore_drill.py",
            "deploy/scripts/sync_standby.py",
            "deploy/scripts/vendor_runtime_reset.py",
            "deploy/systemd/lifecycle.env.example",
            "deploy/systemd/sms-backup.service",
            "deploy/systemd/sms-backup.timer",
            "deploy/systemd/sms-lifecycle-status.service",
            "deploy/systemd/sms-lifecycle-status.timer",
            "deploy/systemd/sms-partition-maintenance.service",
            "deploy/systemd/sms-partition-maintenance.timer",
            "deploy/systemd/sms-restore-drill.service",
            "deploy/systemd/sms-restore-drill.timer",
            "deploy/systemd/security-report-collector.service",
            "deploy/systemd/security-report-collector.timer",
            "backend/app/services/usage_ledger.py",
            "frontend/src/views/ConfigView.vue",
        ]
    )

    assert change.components == frozenset({"api", "web"})
    assert change.risk == "high-risk"
    assert change.high_risk_paths == (
        "backend/app/services/usage_ledger.py",
        "deploy/initdb/01-create-app-role.sh",
        "deploy/lifecycle.server.example.json",
        "deploy/postgres.Dockerfile",
        "deploy/provision-db-roles.sh",
        "deploy/redis.Dockerfile",
        "deploy/scripts/collect_security_daily_evidence.py",
        "deploy/scripts/lifecycle_manager.py",
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/release_manager.py",
        "deploy/scripts/release_manifest.py",
        "deploy/scripts/restore_drill.py",
        "deploy/scripts/sync_standby.py",
        "deploy/scripts/vendor_runtime_reset.py",
        "deploy/systemd/lifecycle.env.example",
        "deploy/systemd/security-report-collector.service",
        "deploy/systemd/security-report-collector.timer",
        "deploy/systemd/sms-backup.service",
        "deploy/systemd/sms-backup.timer",
        "deploy/systemd/sms-lifecycle-status.service",
        "deploy/systemd/sms-lifecycle-status.timer",
        "deploy/systemd/sms-partition-maintenance.service",
        "deploy/systemd/sms-partition-maintenance.timer",
        "deploy/systemd/sms-restore-drill.service",
        "deploy/systemd/sms-restore-drill.timer",
        "frontend/src/views/ConfigView.vue",
    )


def test_public_cutover_keeps_retired_static_paths_as_deletion_tombstones() -> None:
    paths = [
        "AUTOPILOT.md",
        "BOOTSTRAP.md",
        "RELEASE.md",
        "TASKS.md",
        "scripts/verify_milestone.sh",
    ]

    change = classify_public_cutover_paths(paths)

    assert change.components == frozenset()
    assert change.runtime_changed is False
    assert change.risk == "none"
    for path in paths:
        with pytest.raises(ContractError, match="fast update forbidden"):
            classify_changed_paths([path])


def test_public_cutover_does_not_weaken_normal_or_unknown_path_rejection() -> None:
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_changed_paths(["deploy/scripts/release_manager.py"])
    with pytest.raises(ContractError, match="fast update forbidden"):
        classify_public_cutover_paths(["scripts/future_runtime.py"])


@pytest.mark.parametrize("path", ["/backend/app/main.py", "backend/../schema.sql", ""])
def test_rejects_unsafe_changed_paths(path: str) -> None:
    with pytest.raises(ContractError, match="invalid changed path"):
        classify_changed_paths([path])
