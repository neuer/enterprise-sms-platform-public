from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import app.api.messages as messages_module
from app.core.apikey import ApiAppContext, get_api_key_authenticator
from app.core.auth.jwt import JwtClaims
from app.core.errors import ApiError, api_error_handler, validation_error_handler
from app.services.batch_query import BatchAccessScope, BatchNotFound


class FakeKeyAuth:
    async def authenticate(self, key: str) -> ApiAppContext:
        return ApiAppContext(7, "app-iam", "平台部", frozenset({"verify"}))


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims("operator01", "操作员", "平台部", "operator")


class FakeQueryService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def get_batch(self, batch_no: str, scope: BatchAccessScope) -> dict[str, object]:
        self.calls.append(("batch", batch_no, scope))
        if batch_no == "missing":
            raise BatchNotFound
        return {
            "batch_no": batch_no,
            "category": "verify",
            "channel": "api",
            "app_name": "app-iam",
            "creator": None,
            "dept": "平台部",
            "content": "验证码******",
            "status": "completed",
            "deferred_reason": None,
            "resend_of": None,
            "is_test": False,
            "segments": 1,
            "quota_cost": 1,
            "total": 1,
            "removed_freq_limit": 0,
            "delivered": 1,
            "failed": 0,
            "unknown": 0,
            "scheduled_at": None,
            "created_at": datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        }

    async def list_details(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        *,
        status: str | None,
        page: int,
        size: int,
    ) -> dict[str, object]:
        self.calls.append(("details", batch_no, scope, status, page, size))
        return {
            "total": 1,
            "items": [
                {
                    "id": 9,
                    "phone": "138****8000",
                    "status": "delivered",
                    "vendor_task_id": "task-1",
                    "report_desc": "DELIVRD",
                    "report_time": datetime(2026, 7, 11, 8, 1, tzinfo=UTC),
                }
            ],
        }


class FakeAuditor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.calls.append(values)
        if self.fail:
            raise RuntimeError("audit unavailable")


def make_app(auditor: FakeAuditor | None = None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_api_key_authenticator] = FakeKeyAuth
    app.dependency_overrides[messages_module.get_sensitive_read_auditor] = (
        lambda: auditor or FakeAuditor()
    )
    app.include_router(messages_module.router)
    return app


def test_batch_and_detail_queries_are_scoped_and_only_return_phone_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeQueryService()
    monkeypatch.setattr(messages_module, "_batch_queries", lambda: service)
    client = TestClient(make_app())

    batch = client.get("/api/v1/messages/batches/batch-1", headers={"X-Api-Key": "key"})
    details = client.get(
        "/api/v1/messages/batches/batch-1/details?status=delivered&page=2&size=20",
        headers={"X-Api-Key": "key"},
    )

    assert batch.status_code == 200
    assert batch.json()["batch_no"] == "batch-1"
    assert details.status_code == 200
    assert details.json()["items"][0]["phone"] == "138****8000"
    assert "phone_enc" not in details.text
    assert "phone_hmac" not in details.text
    scope = service.calls[0][2]
    assert scope == BatchAccessScope(app_id=7)
    assert service.calls[1][3:] == ("delivered", 2, 20)


def test_missing_batch_uses_uniform_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeQueryService()
    monkeypatch.setattr(messages_module, "_batch_queries", lambda: service)
    response = TestClient(make_app()).get(
        "/api/v1/messages/batches/missing",
        headers={"X-Api-Key": "key"},
    )
    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "批次不存在",
        "detail": None,
    }


def test_batch_content_audit_is_recorded_before_response_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeQueryService()
    monkeypatch.setattr(messages_module, "_batch_queries", lambda: service)
    auditor = FakeAuditor()
    response = TestClient(make_app(auditor)).get(
        "/api/v1/messages/batches/batch-1",
        headers={"X-Api-Key": "key"},
    )
    assert response.status_code == 200
    assert auditor.calls == [
        {
            "action": "batch_content_read",
            "object_type": "batch",
            "object_id": "batch-1",
            "ip": "0.0.0.0",
            "count": 1,
        }
    ]

    failed = TestClient(make_app(FakeAuditor(fail=True)), raise_server_exceptions=False).get(
        "/api/v1/messages/batches/batch-1",
        headers={"X-Api-Key": "key"},
    )
    assert failed.status_code == 500
    assert "验证码" not in failed.text


def test_batch_query_accepts_bearer_and_applies_department_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeQueryService()
    monkeypatch.setattr(messages_module, "_batch_queries", lambda: service)
    monkeypatch.setattr(messages_module, "get_auth_facade", lambda: FakeFacade())
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_api_key_authenticator] = FakeKeyAuth
    app.dependency_overrides[messages_module.get_sensitive_read_auditor] = FakeAuditor
    app.include_router(messages_module.router)

    response = TestClient(app).get(
        "/api/v1/messages/batches/batch-1",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    assert service.calls[0][2] == BatchAccessScope(dept="平台部")
