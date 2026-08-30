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
from app.services.vendor_control_state import VendorControlState  # noqa: E402
from app.services.vendor_test_operation import (  # noqa: E402
    VendorTestOperation,
    VendorTestOperationConflict,
)

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000111"


class FakeFacade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims(
            account_id=8,
            identity_id=18,
            provider_code="local",
            login_name="admin",
            display_name="管理员",
            dept="平台部",
            role="admin",
            security_version=3,
            jti="jwt-session-7",
        )


class FakeState:
    def __init__(self, pause_kind: str | None = None) -> None:
        self.pause_kind = pause_kind

    def read(self) -> VendorControlState:
        return VendorControlState("controlled", NOW, True, 1, self.pause_kind, 100)


class FakeStepUp:
    def __init__(self) -> None:
        self.consumes: list[tuple[str, str]] = []

    async def consume(
        self,
        token: str,
        claims: JwtClaims,
        _ip: str,
        operation: str,
    ) -> None:
        assert claims.login_name == "admin"
        self.consumes.append((token, operation))


class FakeOperations:
    def __init__(
        self,
        *,
        existing: VendorTestOperation | None = None,
        conflict: bool = False,
    ) -> None:
        self.existing = existing
        self.conflict = conflict
        self.started: list[dict[str, object]] = []
        self.background: list[dict[str, object]] = []

    async def start(self, **values: object) -> VendorTestOperation:
        if self.conflict:
            raise VendorTestOperationConflict("another mutation is active")
        self.started.append(values)
        principal = values["principal"]
        assert isinstance(principal, SecurityPrincipal)
        return VendorTestOperation(
            operation_id=OPERATION_ID,
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

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        assert operation_id == OPERATION_ID
        return self.existing

    def reset_recovery_due(self, record: VendorTestOperation) -> bool:
        assert record is self.existing
        return False


def _operation(
    *,
    status: str = "failed",
    safe_code: str | None = "VENDOR_ERROR",
    vendor_code: int | None = 1010,
) -> VendorTestOperation:
    return VendorTestOperation(
        operation_id=OPERATION_ID,
        operation_type="activate",
        actor="admin",
        status=status,
        safe_code=safe_code,
        vendor_code=vendor_code,
        batch_no=None,
        checkpoint_id=None,
        requested_at=NOW,
        completed_at=NOW,
    )


def client(
    *,
    pause_kind: str | None = None,
    existing: VendorTestOperation | None = None,
    conflict: bool = False,
) -> tuple[TestClient, FakeStepUp, FakeOperations]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(vendor_test_api.router)
    step_up = FakeStepUp()
    operations = FakeOperations(existing=existing, conflict=conflict)
    app.dependency_overrides[get_auth_facade] = FakeFacade
    app.dependency_overrides[vendor_test_api.get_vendor_control_state] = lambda: FakeState(
        pause_kind
    )
    app.dependency_overrides[vendor_test_api.get_vendor_step_up_service] = lambda: step_up
    app.dependency_overrides[vendor_test_api.get_vendor_operation_service] = lambda: operations
    return TestClient(app), step_up, operations


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test"}


def _assert_safe(response: Any) -> None:
    rendered = json.dumps(response.json(), ensure_ascii=False).casefold()
    for forbidden in (
        "formal-key-private",
        "13800138000",
        "vendor request",
        "ip check failed from carrier",
    ):
        assert forbidden not in rendered
    assert response.headers["cache-control"] == "no-store"


def test_activate_returns_requested_operation_and_runs_out_of_response_path() -> None:
    http, step_up, operations = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/activate",
        headers=_headers(),
        json={"step_up_token": "step-token"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "requested"
    assert response.json()["operation_id"] == OPERATION_ID
    assert step_up.consumes == [("step-token", "activate")]
    assert operations.started[0]["operation_type"] == "activate"
    assert operations.started[0]["body"] == {}
    assert operations.background[0]["operation_id"] == OPERATION_ID
    _assert_safe(response)

def test_manual_pause_and_resume_are_isolated_and_need_no_step_up() -> None:
    pause_http, _, pause_operations = client()
    paused = pause_http.post(
        "/api/v1/web/admin/vendor-test/pause",
        headers=_headers(),
        json={},
    )
    resume_http, resume_step_up, resume_operations = client(pause_kind="manual")
    resumed = resume_http.post(
        "/api/v1/web/admin/vendor-test/resume",
        headers=_headers(),
        json={},
    )

    assert paused.status_code == resumed.status_code == 202
    assert pause_operations.started[0]["body"] == {"pause_kind": "manual"}
    assert resume_operations.started[0]["body"] == {"pause_kind": "manual"}
    assert resume_step_up.consumes == []


def test_critical_resume_requires_matching_one_use_step_up() -> None:
    http, step_up, operations = client(pause_kind="critical")

    missing = http.post(
        "/api/v1/web/admin/vendor-test/resume",
        headers=_headers(),
        json={},
    )
    resumed = http.post(
        "/api/v1/web/admin/vendor-test/resume",
        headers=_headers(),
        json={"step_up_token": "critical-token"},
    )

    assert missing.status_code == 401
    assert missing.json()["code"] == "STEP_UP_REQUIRED"
    assert resumed.status_code == 202
    assert step_up.consumes == [("critical-token", "resume_critical")]
    assert operations.started[0]["body"] == {"pause_kind": "critical"}


def test_daily_pause_cannot_be_cleared_and_concurrent_mutation_is_rejected() -> None:
    daily_http, *_ = client(pause_kind="daily")
    daily = daily_http.post(
        "/api/v1/web/admin/vendor-test/resume",
        headers=_headers(),
        json={},
    )
    conflict_http, *_ = client(conflict=True)
    conflict = conflict_http.post(
        "/api/v1/web/admin/vendor-test/pause",
        headers=_headers(),
        json={},
    )

    assert daily.status_code == 409
    assert daily.json()["code"] == "STATE_CONFLICT"
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONTROL_OPERATION_IN_PROGRESS"


def test_operation_polling_exposes_only_approved_error_and_integer_vendor_code() -> None:
    http, *_ = client(existing=_operation())

    response = http.get(
        f"/api/v1/web/admin/vendor-test/operations/{OPERATION_ID}",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["safe_code"] == "VENDOR_ERROR"
    assert response.json()["vendor_code"] == 1010
    assert set(response.json()) == {
        "operation_id",
        "operation_type",
        "status",
        "safe_code",
        "vendor_code",
        "batch_no",
        "checkpoint_id",
        "requested_at",
        "completed_at",
    }
    _assert_safe(response)
