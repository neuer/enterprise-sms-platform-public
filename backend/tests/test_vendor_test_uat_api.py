from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from app.api import vendor_test as api  # noqa: E402
from app.core.auth.jwt import JwtClaims  # noqa: E402
from app.core.auth.runtime import get_auth_facade  # noqa: E402
from app.core.errors import ApiError, api_error_handler  # noqa: E402
from app.services.billing_preview import BillingPreview, SegmentPart  # noqa: E402
from app.services.vendor_control_state import (  # noqa: E402
    VendorControlState,
    VendorControlStateUnavailable,
)
from app.services.vendor_test_operation import (  # noqa: E402
    VendorTestOperation,
    VendorTestOperationPending,
)

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000121"


class Facade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims(
            account_id=1,
            identity_id=2,
            provider_code="local",
            login_name="admin",
            display_name="管理员",
            dept="平台部",
            role="admin",
            security_version=1,
            jti="jti-1",
        )


class Uat:
    def __init__(self) -> None:
        self.values: dict[str, object] | None = None

    async def send(self, **values: object) -> VendorTestOperation:
        self.values = values
        return VendorTestOperation(
            OPERATION_ID,
            "uat_send",
            "admin",
            "running",
            None,
            "batch-uat",
            None,
            NOW,
            None,
        )

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        assert operation_id == OPERATION_ID
        return await self.send()

    async def preview(self, **values: object) -> BillingPreview:
        self.values = values
        return BillingPreview(12, 1, 1, [SegmentPart(12, 70, True)], 59, False, False)


class State:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls = 0

    def require_fresh(self) -> VendorControlState:
        self.calls += 1
        if not self.available:
            raise VendorControlStateUnavailable(
                "stale",
                requires_critical_pause=True,
            )
        return VendorControlState("controlled", NOW, True, 1, None, 100)


def client(
    *,
    available: bool = True,
    uat: Uat | None = None,
) -> tuple[TestClient, Uat, State]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    uat = uat or Uat()
    state = State(available=available)
    app.dependency_overrides[get_auth_facade] = Facade
    app.dependency_overrides[api.get_vendor_uat_service] = lambda: uat
    app.dependency_overrides[api.get_vendor_uat_preview_service] = lambda: uat
    app.dependency_overrides[api.get_vendor_control_state] = lambda: state
    return TestClient(app), uat, state


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer jwt"}


def test_uat_api_accepts_recipient_id_only_and_returns_safe_operation() -> None:
    http, uat, state = client()
    payload = {
        "recipient_id": 9,
        "app_id": 7,
        "biz_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "category": "market",
        "content": "活动通知",
        "consent_confirmed": True,
        "remark": "受控单条",
    }

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["operation_id"] == OPERATION_ID
    assert response.json()["batch_no"] == "batch-uat"
    assert response.json()["status"] == "running"
    assert state.calls == 1
    assert uat.values is not None
    assert uat.values["recipient_id"] == 9
    assert uat.values["biz_id"] == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

    missing_key = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json={key: value for key, value in payload.items() if key != "biz_id"},
    )
    assert missing_key.status_code == 400
    rendered = json.dumps(response.json()).casefold()
    for forbidden in ("phone", "mobile", "13800138000", "phone_hmac", "phone_enc"):
        assert forbidden not in rendered

    forbidden = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json=payload | {"mobile": "13800138000"},
    )
    assert forbidden.status_code == 400
    assert forbidden.json() == {
        "code": "INVALID_PARAM",
        "message": "请求参数不合法",
        "detail": None,
    }


def test_uat_preview_uses_selected_app_and_supports_verify_category() -> None:
    http, uat, state = client()

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages/preview",
        headers=headers(),
        json={
            "app_id": 7,
            "category": "verify",
            "content": "验证码123456",
            "consent_confirmed": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["quota_cost"] == 1
    assert uat.values is not None
    assert uat.values["app_id"] == 7
    assert uat.values["category"] == "verify"
    assert state.calls == 1


def test_uat_api_rejects_multiple_or_missing_content_sources() -> None:
    http, *_ = client()
    base = {
        "recipient_id": 9,
        "app_id": 7,
        "biz_id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
        "category": "notice",
    }

    missing = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json=base,
    )
    multiple = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json=base | {"content": "通知", "template_id": 3, "template_params": []},
    )

    assert missing.status_code == multiple.status_code == 400
    assert missing.json() == multiple.json() == {
        "code": "INVALID_PARAM",
        "message": "请求参数不合法",
        "detail": None,
    }


def test_uat_api_rejects_stale_or_blocked_state_before_creating_operation() -> None:
    http, uat, state = client(available=False)

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json={
            "recipient_id": 9,
            "app_id": 7,
            "biz_id": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
            "category": "notice",
            "content": "维护通知",
        },
    )

    assert response.status_code == 503
    assert response.json()["code"] == "CONTROL_AGENT_UNAVAILABLE"
    assert state.calls == 1
    assert uat.values is None


def test_uat_api_returns_same_running_operation_when_send_result_is_pending() -> None:
    class PendingUat(Uat):
        async def send(self, **values: object) -> VendorTestOperation:
            self.values = values
            raise VendorTestOperationPending("private pending detail")

        async def get(self, operation_id: str) -> VendorTestOperation | None:
            assert self.values is not None
            assert operation_id == self.values["operation_id"]
            return VendorTestOperation(
                operation_id,
                "uat_send",
                "admin",
                "running",
                None,
                None,
                None,
                NOW,
                None,
            )

    http, uat, _ = client(uat=PendingUat())

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json={
            "recipient_id": 9,
            "app_id": 7,
            "biz_id": "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
            "category": "notice",
            "content": "维护通知",
        },
    )

    assert response.status_code == 200
    assert uat.values is not None
    assert response.json()["operation_id"] == uat.values["operation_id"]
    assert response.json()["status"] == "running"
    assert response.json()["batch_no"] is None
    assert "private pending detail" not in response.text


def test_uat_api_returns_terminal_operation_restored_during_pending_recovery() -> None:
    class TerminalPendingUat(Uat):
        async def send(self, **values: object) -> VendorTestOperation:
            self.values = values
            raise VendorTestOperationPending("private pending detail")

        async def get(self, operation_id: str) -> VendorTestOperation | None:
            assert self.values is not None
            assert operation_id == self.values["operation_id"]
            return VendorTestOperation(
                operation_id,
                "uat_send",
                "admin",
                "succeeded",
                None,
                "batch-terminal",
                None,
                NOW,
                NOW,
            )

    http, uat, _ = client(uat=TerminalPendingUat())

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json={
            "recipient_id": 9,
            "app_id": 7,
            "biz_id": "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "category": "notice",
            "content": "维护通知",
        },
    )

    assert response.status_code == 200
    assert uat.values is not None
    assert response.json()["operation_id"] == uat.values["operation_id"]
    assert response.json()["status"] == "succeeded"
    assert response.json()["batch_no"] == "batch-terminal"
    assert "private pending detail" not in response.text


def test_uat_api_returns_safe_503_when_pending_operation_cannot_be_restored() -> None:
    class MissingPendingUat(Uat):
        async def send(self, **values: object) -> VendorTestOperation:
            self.values = values
            raise VendorTestOperationPending("private pending detail")

        async def get(self, operation_id: str) -> VendorTestOperation | None:
            assert self.values is not None
            assert operation_id == self.values["operation_id"]
            return None

    http, _, _ = client(uat=MissingPendingUat())

    response = http.post(
        "/api/v1/web/admin/vendor-test/messages",
        headers=headers(),
        json={
            "recipient_id": 9,
            "app_id": 7,
            "biz_id": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
            "category": "notice",
            "content": "维护通知",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "CONTROL_AGENT_UNAVAILABLE",
        "message": "UAT 操作等待安全对账",
        "detail": None,
    }
    assert "private pending detail" not in response.text
