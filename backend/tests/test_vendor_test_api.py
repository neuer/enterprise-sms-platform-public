from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from app.api import vendor_test as vendor_test_api  # noqa: E402
from app.core.auth.accounts import SecurityPrincipal  # noqa: E402
from app.core.auth.jwt import JwtClaims  # noqa: E402
from app.core.auth.runtime import get_auth_facade  # noqa: E402
from app.core.errors import ApiError, api_error_handler  # noqa: E402
from app.services.vendor_control_state import (  # noqa: E402
    VendorControlState,
    VendorControlStateUnavailable,
)
from app.services.vendor_test_operation import (  # noqa: E402
    VendorTestOperation,
    VendorTestOperationConflict,
)
from app.services.vendor_test_recipient import (  # noqa: E402
    RecipientBusy,
    VendorTestRecipientRecord,
    VendorTestRecipientSummary,
)
from vendor_control_protocol import ControlResponse  # noqa: E402

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000101"
CIPHERTEXT = "ciphertext-formal-key-sentinel"
PHONE = "13900000001"


class FakeFacade:
    def __init__(self, role: str = "admin") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims(
            account_id=8,
            identity_id=18,
            provider_code="local",
            login_name="admin",
            display_name="管理员",
            dept="平台部",
            role=self.role,  # type: ignore[arg-type]
            security_version=3,
            jti="jwt-session-7",
        )


class FakeStateGuard:
    def __init__(
        self,
        state: VendorControlState | None = None,
        error: VendorControlStateUnavailable | None = None,
    ) -> None:
        self.state = state or VendorControlState(
            "controlled",
            NOW,
            True,
            1,
            None,
            100,
        )
        self.error = error

    def read(self) -> VendorControlState:
        raise AssertionError("页面状态不得接受过期心跳")

    def read_fresh(self) -> VendorControlState:
        if self.error is not None:
            raise self.error
        return self.state


class FakeStepUp:
    def __init__(
        self,
        issue_error: Exception | None = None,
        consume_error: Exception | None = None,
    ) -> None:
        self.issues: list[tuple[str, str, str]] = []
        self.consumes: list[tuple[str, str, str]] = []
        self.issue_error = issue_error
        self.consume_error = consume_error

    async def issue(
        self,
        *,
        claims: JwtClaims,
        password: str,
        ip: str,
        operation: str,
    ) -> str:
        self.issues.append((claims.provider_code, password, operation))
        if self.issue_error is not None:
            raise self.issue_error
        return "step-up-token"

    async def consume(
        self,
        token: str,
        claims: JwtClaims,
        ip: str,
        operation: str,
    ) -> None:
        self.consumes.append((token, claims.login_name, operation))
        if self.consume_error is not None:
            raise self.consume_error


class FakeControlClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.error = error

    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse:
        self.calls.append((operation, operation_id, body))
        if self.error is not None:
            raise self.error
        return ControlResponse(
            operation_id,
            "ok",
            None,
            {
                "session_id": "seal-session-1",
                "public_key": "public-key-base64",
                "expires_at": NOW.isoformat(),
                "aad": "aad-base64",
            },
        )


class FakeSecurityAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.events.append(values)


class FakeOperations:
    def __init__(self, *, start_conflict: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.started: list[dict[str, object]] = []
        self.background: list[dict[str, object]] = []
        self.start_conflict = start_conflict

    async def execute(self, **values: object) -> VendorTestOperation:
        self.calls.append(values)
        principal = values["principal"]
        assert isinstance(principal, SecurityPrincipal)
        return VendorTestOperation(
            operation_id=str(values["operation_id"]),
            operation_type=str(values["operation_type"]),
            actor=principal.login_name,
            status="succeeded",
            safe_code=None,
            batch_no=None,
            checkpoint_id=None,
            requested_at=NOW,
            completed_at=NOW,
            actor_account_id=principal.account_id,
            actor_identity_id=principal.identity_id,
        )

    async def execute_reserved(self, **values: object) -> VendorTestOperation:
        self.calls.append(values)
        principal = values["principal"]
        assert isinstance(principal, SecurityPrincipal)
        return VendorTestOperation(
            operation_id=str(values["operation_id"]),
            operation_type=str(values["operation_type"]),
            actor=principal.login_name,
            status="succeeded",
            safe_code=None,
            batch_no=None,
            checkpoint_id=None,
            requested_at=NOW,
            completed_at=NOW,
            actor_account_id=principal.account_id,
            actor_identity_id=principal.identity_id,
        )

    async def start(self, **values: object) -> VendorTestOperation:
        if self.start_conflict:
            raise VendorTestOperationConflict("another mutation is active")
        self.started.append(values)
        principal = values["principal"]
        assert isinstance(principal, SecurityPrincipal)
        return VendorTestOperation(
            operation_id=str(values["operation_id"]),
            operation_type=str(values["operation_type"]),
            actor=principal.login_name,
            status="requested",
            safe_code=None,
            vendor_code=None,
            batch_no=None,
            checkpoint_id=None,
            requested_at=NOW,
            completed_at=None,
            actor_account_id=principal.account_id,
            actor_identity_id=principal.identity_id,
        )

    async def execute_background(self, **values: object) -> None:
        self.background.append(values)


class FakeRecipients:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []
        self.disabled: list[tuple[int, str]] = []
        self.refreshed: list[tuple[int, str, str]] = []

    async def add(self, *, label: str, phone: str, actor: str) -> VendorTestRecipientRecord:
        self.added.append((label, phone, actor))
        return VendorTestRecipientRecord(
            7,
            label,
            b"ciphertext-only",
            "a" * 64,
            "139****0001",
            2,
            "active",
            actor,
            NOW,
        )

    async def list(self, *, include_disabled: bool = True):
        return (
            VendorTestRecipientSummary(7, "值班机", "139****0001", "active", NOW),
        )

    async def disable(self, recipient_id: int, *, actor: str):
        self.disabled.append((recipient_id, actor))
        return VendorTestRecipientSummary(
            recipient_id,
            "值班机",
            "139****0001",
            "disabled",
            NOW,
            NOW,
        )

    async def refresh_hmac_index(
        self,
        recipient_id: int,
        *,
        phone: str,
        actor: str,
    ):
        self.refreshed.append((recipient_id, phone, actor))
        return VendorTestRecipientSummary(
            recipient_id,
            "值班机",
            "139****0001",
            "active",
            NOW,
        )


def client(
    role: str = "admin",
    *,
    step_up_error: Exception | None = None,
    consume_error: Exception | None = None,
    control_error: Exception | None = None,
    control_state: VendorControlState | None = None,
    state_error: VendorControlStateUnavailable | None = None,
    start_conflict: bool = False,
) -> tuple[
    TestClient,
    FakeStepUp,
    FakeControlClient,
    FakeOperations,
    FakeRecipients,
]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(vendor_test_api.router)
    step_up = FakeStepUp(step_up_error, consume_error)
    control = FakeControlClient(control_error)
    security_audit = FakeSecurityAudit()
    operations = FakeOperations(start_conflict=start_conflict)
    recipients = FakeRecipients()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[vendor_test_api.get_vendor_control_state] = lambda: (
        FakeStateGuard(control_state, state_error)
    )
    app.dependency_overrides[vendor_test_api.get_vendor_step_up_service] = lambda: step_up
    app.dependency_overrides[vendor_test_api.get_vendor_control_client] = lambda: control
    app.dependency_overrides[vendor_test_api.get_vendor_security_audit] = (
        lambda: security_audit
    )
    app.dependency_overrides[vendor_test_api.get_vendor_operation_service] = lambda: operations
    app.dependency_overrides[vendor_test_api.get_vendor_recipient_service] = lambda: recipients
    app.state.vendor_test_security_audit = security_audit
    return TestClient(app), step_up, control, operations, recipients


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test"}


def _assert_safe(response: Any) -> None:
    rendered = json.dumps(response.json()).casefold()
    for token in (
        "secretname",
        "secret_name",
        "secretkey",
        "secret_key",
        "phone_hmac",
        "phone_enc",
        CIPHERTEXT.casefold(),
        PHONE,
    ):
        assert token not in rendered
    assert response.headers["cache-control"] == "no-store"


def test_non_admin_is_forbidden_before_any_vendor_test_dependency_action() -> None:
    http, step_up, control, operations, recipients = client("viewer")

    response = http.get("/api/v1/web/admin/vendor-test/status", headers=_headers())

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert not step_up.issues and not control.calls and not operations.calls
    assert not recipients.added
    _assert_safe(response)


def test_status_is_safe_projection_and_never_cacheable() -> None:
    http, *_ = client()

    response = http.get("/api/v1/web/admin/vendor-test/status", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {
        "mode": "controlled",
        "heartbeat_at": NOW.isoformat().replace("+00:00", "Z"),
        "credential_configured": True,
        "active_recipient_count": 1,
        "pause_kind": None,
        "daily_limit": 100,
    }
    _assert_safe(response)


def test_step_up_uses_current_provider_and_response_never_contains_password() -> None:
    http, step_up, *_ = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "install_credentials", "password": "Current@Password123"},
    )

    assert response.status_code == 200
    assert response.json() == {"token": "step-up-token", "expires_in": 300}
    assert step_up.issues == [
        ("local", "Current@Password123", "install_credentials")
    ]
    assert "Current@Password123" not in response.text
    audit = http.app.state.vendor_test_security_audit
    assert audit.events[0]["action"] == "vendor_test_step_up"
    assert audit.events[0]["outcome"] == "succeeded"
    assert "Current@Password123" not in repr(audit.events)
    assert "step-up-token" not in repr(audit.events)
    _assert_safe(response)


def test_reset_step_up_invalid_password_preserves_current_bearer_session() -> None:
    rejected = ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None)
    http, step_up, *_ = client(step_up_error=rejected)

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "reset_configuration", "password": "wrong-password"},
    )
    status = http.get("/api/v1/web/admin/vendor-test/status", headers=_headers())

    assert response.status_code == 401
    assert response.json()["code"] == "STEP_UP_REQUIRED"
    assert status.status_code == 200
    assert step_up.issues == [("local", "wrong-password", "reset_configuration")]
    _assert_safe(response)


def test_step_up_account_lock_uses_explicit_423_contract() -> None:
    rejected = ApiError(423, "ACCOUNT_LOCKED", "账号已临时锁定", None)
    http, *_ = client(step_up_error=rejected)

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "reset_configuration", "password": "wrong-password"},
    )

    assert response.status_code == 423
    assert response.json()["code"] == "ACCOUNT_LOCKED"
    _assert_safe(response)


def test_reset_accepts_inactive_state_and_runs_empty_body_in_background() -> None:
    state = VendorControlState("inactive", NOW, True, 3, None, 100)
    http, step_up, _, operations, _ = client(control_state=state)

    response = http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={"step_up_token": "reset-step-token"},
    )

    assert response.status_code == 202
    assert response.json()["operation_type"] == "reset_configuration"
    assert response.json()["status"] == "requested"
    assert step_up.consumes == [
        ("reset-step-token", "admin", "reset_configuration")
    ]
    assert operations.started[0]["operation_type"] == "reset_configuration"
    assert operations.started[0]["principal"] == SecurityPrincipal(
        8, 18, "admin", "平台部", "admin"
    )
    assert operations.started[0]["body"] == {}
    assert response.json()["operation_id"] == operations.started[0]["operation_id"]
    assert operations.background == [operations.started[0]]
    assert "purge" not in response.text.casefold()
    _assert_safe(response)


def test_reset_rejects_generic_setup_required_without_starting_new_operation() -> None:
    state = VendorControlState("setup_required", NOW, False, 2, None, 100)
    http, step_up, _, operations, _ = client(control_state=state)

    response = http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={"step_up_token": "recovery-token"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
    assert step_up.consumes == [("recovery-token", "admin", "reset_configuration")]
    assert operations.started == []
    assert operations.background == []
    _assert_safe(response)


def test_reset_rejects_unsafe_or_inconsistent_control_projection() -> None:
    states = (
        VendorControlState("controlled", NOW, True, 1, None, 100),
        VendorControlState("blocked", NOW, True, 1, "critical", 100),
        VendorControlState("inactive", NOW, False, 1, None, 100),
        VendorControlState("inactive", NOW, True, 1, "manual", 100),
        VendorControlState("setup_required", NOW, True, 1, None, 100),
        VendorControlState("setup_required", NOW, False, 1, "critical", 100),
    )

    for state in states:
        http, step_up, _, operations, _ = client(control_state=state)
        response = http.post(
            "/api/v1/web/admin/vendor-test/reset",
            headers=_headers(),
            json={"step_up_token": "unsafe-state-token"},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "STATE_CONFLICT"
        assert step_up.consumes == [
            ("unsafe-state-token", "admin", "reset_configuration")
        ]
        assert operations.started == []
        _assert_safe(response)


def test_reset_fails_closed_for_unreadable_state_or_pending_operation() -> None:
    unavailable = VendorControlStateUnavailable(
        "stale private projection",
        requires_critical_pause=True,
    )
    stale_http, stale_step_up, _, stale_operations, _ = client(
        state_error=unavailable
    )
    stale = stale_http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={"step_up_token": "stale-state-token"},
    )
    conflict_http, conflict_step_up, _, conflict_operations, _ = client(
        control_state=VendorControlState("inactive", NOW, True, 1, None, 100),
        start_conflict=True,
    )
    conflict = conflict_http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={"step_up_token": "conflict-token"},
    )

    assert stale.status_code == 503
    assert stale.json()["code"] == "CONTROL_AGENT_UNAVAILABLE"
    assert stale_step_up.consumes == [
        ("stale-state-token", "admin", "reset_configuration")
    ]
    assert stale_operations.started == []
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONTROL_OPERATION_IN_PROGRESS"
    assert conflict_step_up.consumes == [
        ("conflict-token", "admin", "reset_configuration")
    ]
    assert conflict_operations.background == []
    _assert_safe(stale)
    _assert_safe(conflict)


def test_reset_request_is_strict_admin_only_and_never_exposes_sensitive_fields() -> None:
    state = VendorControlState("inactive", NOW, True, 1, None, 100)
    http, step_up, _, operations, _ = client(control_state=state)
    invalid = http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={
            "step_up_token": "reset-step-token",
            "secretKey": "forbidden",
            "phone": PHONE,
            "count": 1,
        },
    )
    viewer_http, viewer_step_up, _, viewer_operations, _ = client(
        "viewer",
        control_state=state,
    )
    forbidden = viewer_http.post(
        "/api/v1/web/admin/vendor-test/reset",
        headers=_headers(),
        json={"step_up_token": "viewer-token"},
    )

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_PARAM"
    assert step_up.consumes == [] and operations.started == []
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    assert viewer_step_up.consumes == [] and viewer_operations.started == []
    _assert_safe(invalid)
    _assert_safe(forbidden)


def test_seal_session_returns_only_one_use_public_material() -> None:
    http, _, control, *_ = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/seal-sessions",
        headers=_headers(),
        json={"operation": "install_credentials"},
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == "seal-session-1"
    assert response.json()["public_key"] == "public-key-base64"
    assert control.calls[0][0] == "create_seal_session"
    assert control.calls[0][2] == {
        "operation": "install_credentials",
        "actor": "admin",
    }
    audit = http.app.state.vendor_test_security_audit
    assert audit.events[0]["action"] == "vendor_test_seal_session"
    assert audit.events[0]["outcome"] == "succeeded"
    assert "public-key-base64" not in repr(audit.events)
    _assert_safe(response)


def test_step_up_and_seal_failures_write_safe_audit_rows() -> None:
    from app.services.vendor_control_client import ControlAgentUnavailable

    step_http, *_ = client(step_up_error=RuntimeError("private-password-detail"))
    step_response = step_http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "activate", "password": "Current@Password123"},
    )
    seal_http, *_ = client(
        control_error=ControlAgentUnavailable("private-socket-detail")
    )
    seal_response = seal_http.post(
        "/api/v1/web/admin/vendor-test/seal-sessions",
        headers=_headers(),
        json={"operation": "install_credentials"},
    )

    assert step_response.status_code == 500
    assert seal_response.status_code == 503
    step_audit = step_http.app.state.vendor_test_security_audit.events
    seal_audit = seal_http.app.state.vendor_test_security_audit.events
    assert step_audit[0]["safe_code"] == "STEP_UP_REJECTED"
    assert seal_audit[0]["safe_code"] == "CONTROL_AGENT_UNAVAILABLE"
    assert step_audit[0]["outcome"] == seal_audit[0]["outcome"] == "failed"
    rendered = repr((step_audit, seal_audit))
    assert "Current@Password123" not in rendered
    assert "private" not in rendered


def test_credentials_accept_only_ciphertext_consume_step_up_and_return_operation() -> None:
    http, step_up, _, operations, _ = client()
    payload = {
        "operation": "install_credentials",
        "step_up_token": "step-up-token",
        "session_id": "seal-session-1",
        "wrapped_key": "wrapped-key-base64",
        "nonce": "nonce-base64",
        "ciphertext": CIPHERTEXT,
        "aad": "aad-base64",
        "algorithm": "RSA-OAEP-256+A256GCM",
    }

    response = http.put(
        "/api/v1/web/admin/vendor-test/credentials",
        headers=_headers(),
        json=payload,
    )

    assert response.status_code == 200
    assert step_up.consumes == [("step-up-token", "admin", "install_credentials")]
    assert operations.started[0] == {
        "operation_id": operations.calls[0]["operation_id"],
        "operation_type": "install_credentials",
        "principal": SecurityPrincipal(8, 18, "admin", "平台部", "admin"),
        "body": {},
    }
    assert operations.calls[0]["body"] == {
        key: value
        for key, value in payload.items()
        if key not in {"operation", "step_up_token"}
    }
    assert response.json()["status"] == "succeeded"
    _assert_safe(response)

    plaintext = http.put(
        "/api/v1/web/admin/vendor-test/credentials",
        headers=_headers(),
        json=payload | {"secretName": "forbidden", "secretKey": "forbidden"},
    )
    assert plaintext.status_code == 400
    assert plaintext.json()["code"] == "INVALID_PARAM"
    _assert_safe(plaintext)


def test_credentials_conflict_consumes_step_up_but_never_executes_ciphertext() -> None:
    http, step_up, _, operations, _ = client(start_conflict=True)

    response = http.put(
        "/api/v1/web/admin/vendor-test/credentials",
        headers=_headers(),
        json={
            "operation": "rotate_credentials",
            "step_up_token": "one-use-token",
            "session_id": "seal-session-1",
            "wrapped_key": "wrapped-key-base64",
            "nonce": "nonce-base64",
            "ciphertext": CIPHERTEXT,
            "aad": "aad-base64",
            "algorithm": "RSA-OAEP-256+A256GCM",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CONTROL_OPERATION_IN_PROGRESS"
    assert step_up.consumes == [("one-use-token", "admin", "rotate_credentials")]
    assert operations.started == []
    assert operations.calls == []
    _assert_safe(response)


def test_recipient_add_list_disable_returns_only_masked_projection() -> None:
    http, *_, recipients = client()

    added = http.post(
        "/api/v1/web/admin/vendor-test/recipients",
        headers=_headers(),
        json={"label": "值班机", "phone": PHONE},
    )
    listed = http.get(
        "/api/v1/web/admin/vendor-test/recipients",
        headers=_headers(),
    )
    disabled = http.delete(
        "/api/v1/web/admin/vendor-test/recipients/7",
        headers=_headers(),
    )

    assert [added.status_code, listed.status_code, disabled.status_code] == [200, 200, 200]
    assert recipients.added == [("值班机", PHONE, "admin")]
    assert recipients.disabled == [(7, "admin")]
    assert added.json()["phone_mask"] == "139****0001"
    assert disabled.json()["status"] == "disabled"
    for response in (added, listed, disabled):
        _assert_safe(response)


def test_recipient_add_maps_nonterminal_reset_conflict_to_safe_409() -> None:
    class BusyRecipients(FakeRecipients):
        async def add(
            self,
            *,
            label: str,
            phone: str,
            actor: str,
        ) -> VendorTestRecipientRecord:
            del label, phone, actor
            raise RecipientBusy("private reset state")

    http, *_ = client()
    http.app.dependency_overrides[
        vendor_test_api.get_vendor_recipient_service
    ] = BusyRecipients

    response = http.post(
        "/api/v1/web/admin/vendor-test/recipients",
        headers=_headers(),
        json={"label": "值班机", "phone": PHONE},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "RECIPIENT_BUSY",
        "message": "已有真实联调控制操作正在执行",
        "detail": None,
    }
    assert "private reset state" not in response.text
    _assert_safe(response)


def test_recipient_hmac_index_refresh_reenters_phone_but_returns_only_mask() -> None:
    http, *_, recipients = client()

    refreshed = http.post(
        "/api/v1/web/admin/vendor-test/recipients/7/refresh-index",
        headers=_headers(),
        json={"phone": PHONE},
    )

    assert refreshed.status_code == 200
    assert recipients.refreshed == [(7, PHONE, "admin")]
    assert refreshed.json()["phone_mask"] == "139****0001"
    _assert_safe(refreshed)


def test_all_vendor_test_write_routes_declare_stable_audit_actions() -> None:
    expected = {
        vendor_test_api.step_up: "vendor_test_step_up",
        vendor_test_api.create_seal_session: "vendor_test_seal_session",
        vendor_test_api.install_credentials: "vendor_test_credentials",
        vendor_test_api.add_recipient: "vendor_test_recipient_add",
        vendor_test_api.disable_recipient: "vendor_test_recipient_disable",
        vendor_test_api.refresh_recipient_hmac_index: "vendor_test_recipient_refresh_index",
        vendor_test_api.reset_configuration: "vendor_test_reset",
    }

    assert {
        function: getattr(function, "__audited_action__", None)
        for function in expected
    } == expected


def test_openapi_declares_strict_reset_contract_and_operation_enums() -> None:
    contract = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    path = contract.split(
        "  /api/v1/web/admin/vendor-test/reset:\n",
        maxsplit=1,
    )[1].split("\n  /api/v1/", maxsplit=1)[0]
    step_up_path = contract.split(
        "  /api/v1/web/admin/vendor-test/step-up:\n",
        maxsplit=1,
    )[1].split("\n  /api/v1/", maxsplit=1)[0]
    request_schema = contract.split(
        "    VendorTestResetRequestModel:\n",
        maxsplit=1,
    )[1].split("\n    PauseRequestModel:", maxsplit=1)[0]
    step_up_schema = contract.split(
        "    StepUpRequestModel:\n",
        maxsplit=1,
    )[1].split("\n    StepUpResponseModel:", maxsplit=1)[0]
    operation_schema = contract.split(
        "    VendorTestOperationModel:\n",
        maxsplit=1,
    )[1].split("\n    ActivateRequestModel:", maxsplit=1)[0]

    assert "'202':" in path
    assert "VendorTestResetRequestModel" in path
    for status in ("'401':", "'403':", "'409':", "'503':"):
        assert status in path
    assert "'423':" in step_up_path
    assert "additionalProperties: false" in request_schema
    assert "required: [step_up_token]" in request_schema
    assert "step_up_token:" in request_schema
    for forbidden in ("secret", "phone", "count"):
        assert forbidden not in request_schema.replace("step_up_token", "").casefold()
    assert "reset_configuration" in step_up_schema
    assert "reset_configuration" in operation_schema


def test_openapi_declares_credential_lifecycle_conflict() -> None:
    contract = (ROOT / "openapi.yaml").read_text(encoding="utf-8")
    path = contract.split(
        "  /api/v1/web/admin/vendor-test/credentials:\n",
        maxsplit=1,
    )[1].split("\n  /api/v1/", maxsplit=1)[0]

    assert "'409':" in path
    assert "CONTROL_OPERATION_IN_PROGRESS" in path


def test_request_size_limit_runs_before_body_validation_or_service_call() -> None:
    http, step_up, *_ = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "activate", "password": "x" * 20_000},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert step_up.issues == []


def test_request_size_limit_counts_streamed_body_without_content_length() -> None:
    http, step_up, *_ = client()

    def oversized_chunks():
        yield b'{"operation":"activate","password":"'
        yield b"x" * 20_000
        yield b'"}'

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers={**_headers(), "Transfer-Encoding": "chunked"},
        content=oversized_chunks(),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert step_up.issues == []
    _assert_safe(response)


def test_validation_errors_are_never_cacheable() -> None:
    http, step_up, *_ = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/step-up",
        headers=_headers(),
        json={"operation": "activate"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert step_up.issues == []
    _assert_safe(response)
