from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.approvals as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.approval import SelfApprovalDenied


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
                    "created_at": datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
                }
            ],
        }


def test_approval_api_maps_self_approval_to_platform_error() -> None:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_approval_service] = lambda: FakeService()
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
