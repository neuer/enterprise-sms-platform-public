from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import app.api.approvals as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler, validation_error_handler
from app.services.approval import (
    ApprovalCase,
    ApprovalService,
    SelfApprovalDenied,
)


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims(
            11,
            101,
            "local",
            "operator01",
            "操作员",
            "平台部",
            "approver",
        )


class FakeService:
    async def decide(self, *args: object, **kwargs: object) -> None:
        raise SelfApprovalDenied("不能审批本人提交")


class FakeApprovalRepository:
    def __init__(self) -> None:
        self.depts: list[str | None] = []

    async def list_page(
        self, *, status: str, dept: str | None, page: int
    ) -> dict[str, object]:
        self.depts.append(dept)
        return {
            "total": 1,
            "items": [
                {
                    "id": 9,
                    "batch_no": "batch-9",
                    "category": "market",
                    "applicant": "operator01",
                    "dept": "市场部",
                    "total": 60,
                    "segments": 2,
                    "estimated_segments": 120,
                    "scheduled_at": datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
                    "trigger_threshold": 50,
                    "trigger_threshold_source": "snapshot",
                    "content": "活动回T退订",
                    "status": "pending",
                    "approver": None,
                    "reason": None,
                    "expires_at": datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
                    "decided_at": None,
                    "created_at": datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
                }
            ],
        }


class FakeAuditor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.calls.append(values)


def make_app(service: object) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_approval_service] = lambda: service
    return app


def test_approval_api_maps_self_approval_to_platform_error() -> None:
    app = make_app(FakeService())
    response = TestClient(app).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "approve"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "SELF_APPROVAL_DENIED"


def test_approver_list_has_all_department_scope() -> None:
    repository = FakeApprovalRepository()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_approval_repository] = lambda: repository
    auditor = FakeAuditor()
    app.dependency_overrides[api.get_sensitive_read_auditor] = lambda: auditor

    response = TestClient(app).get(
        "/api/v1/web/approvals",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    assert repository.depts == [None]
    item = response.json()["items"][0]
    assert item["segments"] == 2
    assert item["estimated_segments"] == 120
    assert item["scheduled_at"] == "2026-07-21T01:00:00Z"
    assert item["trigger_threshold"] == 50
    assert item["trigger_threshold_source"] == "snapshot"
    assert item["expires_at"] == "2026-07-21T01:00:00Z"
    assert item["decided_at"] is None
    assert auditor.calls[0]["action"] == "approval_content_read"
    assert auditor.calls[0]["count"] == 1


class RecordingRepository:
    def __init__(self) -> None:
        self.case = ApprovalCase(
            3,
            "batch-1",
            "applicant01",
            7,
            "平台部",
            "20260720",
            120,
            "market",
            "pending",
            "pending_approval",
            applicant_account_id=99,
            applicant_identity_id=199,
        )
        self.transitioned: list[tuple[str, str | None]] = []

    async def get(self, approval_id: int) -> ApprovalCase | None:
        return self.case if approval_id == self.case.approval_id else None

    async def transition(
        self,
        approval_id: int,
        *,
        action: str,
        principal: object,
        reason: str | None,
    ) -> ApprovalCase | None:
        self.transitioned.append((action, reason))
        return self.case

    async def expire_due(self) -> list[ApprovalCase]:
        return []


class NoopPort:
    async def refund_once(self, **values: object) -> object:
        return object()

    async def enqueue(self, batch_no: str, queue: str) -> None:
        return None

    async def emit(self, **values: object) -> None:
        return None


def make_real_service(repository: RecordingRepository) -> ApprovalService:
    return ApprovalService(repository, NoopPort(), NoopPort(), NoopPort())


def test_reject_with_blank_reason_maps_to_invalid_param() -> None:
    repository = RecordingRepository()
    response = TestClient(make_app(make_real_service(repository))).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "reject", "reason": "   "},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert repository.transitioned == []


def test_reason_over_database_column_limit_maps_to_invalid_param() -> None:
    repository = RecordingRepository()
    response = TestClient(make_app(make_real_service(repository))).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "reject", "reason": "长" * 257},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert repository.transitioned == []


def test_reason_is_trimmed_before_state_transition() -> None:
    repository = RecordingRepository()
    response = TestClient(make_app(make_real_service(repository))).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "reject", "reason": "  内容不合规  "},
    )
    assert response.status_code == 200
    assert repository.transitioned == [("reject", "内容不合规")]
