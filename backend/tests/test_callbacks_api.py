from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.apps as apps_api
import app.api.callbacks as api
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.callback_repository import (
    CallbackRetryConflict,
    CallbackTaskNotFound,
)


class FakeFacade:
    def __init__(self, role: str) -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(1, 10, "local", "admin01", "管理员", "平台部", self.role)  # type: ignore[arg-type]


class FakeRepository:
    def __init__(self, retry_error: Exception | None = None) -> None:
        self.retry_error = retry_error
        self.list_calls: list[dict[str, object]] = []
        self.retry_calls: list[tuple[int, SecurityPrincipal]] = []

    async def list_page(self, **values: object) -> dict[str, object]:
        self.list_calls.append(values)
        return {
            "total": 1,
            "items": [
                {
                    "id": 9,
                    "event_id": "10000000-0000-4000-8000-000000000009",
                    "correlation_id": "30000000-0000-4000-8000-000000000009",
                    "app_id": 7,
                    "app_name": "IAM",
                    "event": "batch.finished",
                    "batch_no": "BATCH-1",
                    "reference_count": 0,
                    "status": "dead",
                    "retry_count": 5,
                    "next_retry_at": None,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "takeover_count": 1,
                    "stalled": False,
                    "last_http_code": 500,
                    "last_error": "TimeoutError",
                    "created_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                    "finished_at": datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
                }
            ],
        }

    async def manual_retry(
        self,
        task_id: int,
        *,
        principal: SecurityPrincipal,
    ) -> None:
        self.retry_calls.append((task_id, principal))
        if self.retry_error is not None:
            raise self.retry_error


def client(repository: FakeRepository, role: str = "admin") -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[api.get_callback_repository] = lambda: repository
    return TestClient(app)


def test_admin_lists_safe_callback_task_summary_and_non_admin_is_forbidden() -> None:
    repository = FakeRepository()
    headers = {"Authorization": "Bearer jwt"}
    response = client(repository).get(
        "/api/v1/web/admin/callbacks?status=dead&app_id=7&page=1",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "dead"
    assert "url" not in response.text and "body" not in response.text
    assert repository.list_calls == [{"status": "dead", "app_id": 7, "page": 1}]
    assert client(FakeRepository(), "operator").get(
        "/api/v1/web/admin/callbacks", headers=headers
    ).status_code == 403


def test_manual_retry_maps_missing_and_state_conflict() -> None:
    headers = {"Authorization": "Bearer jwt"}
    success = FakeRepository()
    assert client(success).post(
        "/api/v1/web/admin/callbacks/9/retry", headers=headers
    ).status_code == 200
    assert success.retry_calls[0][0] == 9
    assert success.retry_calls[0][1].account_id == 1
    assert client(FakeRepository(CallbackTaskNotFound("不存在"))).post(
        "/api/v1/web/admin/callbacks/9/retry", headers=headers
    ).status_code == 404
    assert client(FakeRepository(CallbackRetryConflict("仅 dead 可重推"))).post(
        "/api/v1/web/admin/callbacks/9/retry", headers=headers
    ).status_code == 409


def test_callback_writes_and_secret_rotation_are_audited() -> None:
    assert vars(api.retry_callback)["__audited_action__"] == "callback_retry"
    assert (
        vars(apps_api.rotate_callback_secret)["__audited_action__"]
        == "app_rotate_callback_secret"
    )
