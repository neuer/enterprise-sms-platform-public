from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import app.api.apps as apps_api
import app.api.callbacks as api
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import (
    ApiError,
    api_error_handler,
    validation_error_handler,
)
from app.services.callback_batch_event import FinishedRecovery, FinishedRecoveryRejected
from app.services.callback_repository import CallbackTaskNotFound
from app.services.callback_retry_eligibility import CallbackRetryConflict


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
            "dead_total": 1,
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
                    "manual_retry_allowed": False,
                    "retry_block_reason": "config_revoked",
                    "finished_snapshot": "current",
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


class FakeRecovery:
    def __init__(
        self,
        result: FinishedRecovery | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[int, SecurityPrincipal]] = []

    async def recover(self, task_id: int, *, principal: SecurityPrincipal) -> FinishedRecovery:
        self.calls.append((task_id, principal))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def client(
    repository: FakeRepository,
    role: str = "admin",
    recovery: FakeRecovery | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[api.get_callback_repository] = lambda: repository
    if recovery is not None:
        app.dependency_overrides[api.get_finished_recovery] = lambda: recovery
    return TestClient(app)


def test_admin_lists_safe_callback_task_summary_and_non_admin_is_forbidden() -> None:
    repository = FakeRepository()
    headers = {"Authorization": "Bearer jwt"}
    response = client(repository).get(
        "/api/v1/web/admin/callbacks"
        "?status=dead&app_id=7&event=batch.finished&batch_no=BATCH&page=1&size=50",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "dead"
    assert response.json()["dead_total"] == 1
    assert "url" not in response.text and "body" not in response.text
    assert repository.list_calls == [
        {
            "status": "dead",
            "app_id": 7,
            "event": "batch.finished",
            "batch_no": "BATCH",
            "page": 1,
            "size": 50,
        }
    ]
    assert (
        client(FakeRepository(), "operator")
        .get("/api/v1/web/admin/callbacks", headers=headers)
        .status_code
        == 403
    )


def test_callback_list_rejects_invalid_filters() -> None:
    headers = {"Authorization": "Bearer jwt"}
    bad_event = client(FakeRepository()).get(
        "/api/v1/web/admin/callbacks?event=batch.started",
        headers=headers,
    )
    bad_size = client(FakeRepository()).get(
        "/api/v1/web/admin/callbacks?size=101",
        headers=headers,
    )

    assert bad_event.status_code == 400
    assert bad_event.json()["code"] == "INVALID_PARAM"
    assert bad_size.status_code == 400
    assert bad_size.json()["code"] == "INVALID_PARAM"


def test_manual_retry_maps_missing_and_state_conflict() -> None:
    headers = {"Authorization": "Bearer jwt"}
    success = FakeRepository()
    assert (
        client(success).post("/api/v1/web/admin/callbacks/9/retry", headers=headers).status_code
        == 200
    )
    assert success.retry_calls[0][0] == 9
    assert success.retry_calls[0][1].account_id == 1
    assert (
        client(FakeRepository(CallbackTaskNotFound("不存在")))
        .post("/api/v1/web/admin/callbacks/9/retry", headers=headers)
        .status_code
        == 404
    )
    conflict = client(
        FakeRepository(
            CallbackRetryConflict(
                "旧回调已因配置变更撤销，恢复配置也不能重推",
                "config_revoked",
            )
        )
    ).post("/api/v1/web/admin/callbacks/9/retry", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"reason": "config_revoked"}
    assert "恢复配置也不能重推" in conflict.json()["message"]
    assert "url" not in conflict.text


def test_recover_finished_returns_new_event_and_maps_conflicts() -> None:
    headers = {"Authorization": "Bearer jwt"}
    event_id = UUID("40000000-0000-4000-8000-000000000021")
    recovery = FakeRecovery(FinishedRecovery(21, event_id, True))
    created = client(FakeRepository(), recovery=recovery).post(
        "/api/v1/web/admin/callbacks/9/recover-finished",
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json() == {
        "task_id": 21,
        "event_id": str(event_id),
        "created": True,
    }
    assert recovery.calls[0][0] == 9
    assert recovery.calls[0][1].account_id == 1
    missing = client(
        FakeRepository(),
        recovery=FakeRecovery(
            error=FinishedRecoveryRejected("回调任务不存在", "not_found", missing=True)
        ),
    ).post("/api/v1/web/admin/callbacks/9/recover-finished", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"reason": "not_found"}
    conflict = client(
        FakeRepository(),
        recovery=FakeRecovery(
            error=FinishedRecoveryRejected("这条批次终态已有冻结汇总，不能另补一条", "not_legacy")
        ),
    ).post("/api/v1/web/admin/callbacks/9/recover-finished", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"reason": "not_legacy"}
    assert "url" not in conflict.text and "phone" not in conflict.text
    forbidden = client(FakeRepository(), "operator").post(
        "/api/v1/web/admin/callbacks/9/recover-finished",
        headers=headers,
    )
    assert forbidden.status_code == 403


def test_callback_writes_and_secret_rotation_are_audited() -> None:
    assert vars(api.retry_callback)["__audited_action__"] == "callback_retry"
    assert vars(api.recover_finished_callback)["__audited_action__"] == "callback_finished_recover"
    assert (
        vars(apps_api.rotate_callback_secret)["__audited_action__"] == "app_rotate_callback_secret"
    )
