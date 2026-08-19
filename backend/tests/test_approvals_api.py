from __future__ import annotations

from dataclasses import replace
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


class OperatorFacade:
    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims(
            11,
            101,
            "local",
            "operator01",
            "操作员",
            "平台部",
            "operator",
        )


class FakeService:
    async def decide(self, *args: object, **kwargs: object) -> None:
        raise SelfApprovalDenied("不能审批本人提交")


def sample_item() -> dict[str, object]:
    return {
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
        "batch_status": "pending_approval",
        "deferred_reason": None,
        "status": "pending",
        "approver": None,
        "reason": None,
        "expires_at": datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
        "decided_at": None,
        "created_at": datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
    }


class FakeApprovalRepository:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.count_calls: list[dict[str, object]] = []
        self.detail_ids: list[int] = []

    async def list_page(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        return {"total": 1, "items": [sample_item()]}

    async def counts(self, **kwargs: object) -> dict[str, int]:
        self.count_calls.append(kwargs)
        return {
            "pending": 3,
            "approved": 5,
            "rejected": 2,
            "expired": 1,
            "pending_urgent": 2,
        }

    async def get_detail(self, approval_id: int) -> dict[str, object] | None:
        self.detail_ids.append(approval_id)
        if approval_id != 9:
            return None
        return {**sample_item(), "content": "活动回T退订"}


class FakeAuditor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.calls.append(values)


def register_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]


def make_app(service: object) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_approval_service] = lambda: service
    return app


def make_query_app(
    repository: FakeApprovalRepository, auditor: FakeAuditor
) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_approval_repository] = lambda: repository
    app.dependency_overrides[api.get_sensitive_read_auditor] = lambda: auditor
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


def test_non_approver_is_forbidden() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    app = make_query_app(repository, auditor)
    app.dependency_overrides[get_auth_facade] = lambda: OperatorFacade()
    response = TestClient(app).get(
        "/api/v1/web/approvals",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert repository.list_calls == []


def test_list_response_carries_counts_and_never_decrypts_content() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["counts"] == {
        "pending": 3,
        "approved": 5,
        "rejected": 2,
        "expired": 1,
        "pending_urgent": 2,
    }
    item = body["items"][0]
    assert "content" not in item
    assert item["batch_status"] == "pending_approval"
    assert item["deferred_reason"] is None
    assert item["segments"] == 2
    assert item["estimated_segments"] == 120
    assert item["scheduled_at"] == "2026-07-21T01:00:00Z"
    assert item["trigger_threshold"] == 50
    assert item["trigger_threshold_source"] == "snapshot"
    assert item["expires_at"] == "2026-07-21T01:00:00Z"
    assert item["decided_at"] is None
    # 列表已无正文，不得产生正文敏感读审计
    assert auditor.calls == []


def test_list_defaults_pending_to_expiring_first_and_keeps_all_dept_scope() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert repository.list_calls == [
        {
            "status": "pending",
            "dept": None,
            "category": None,
            "q": None,
            "sort": "expires_asc",
            "page": 1,
            "size": 20,
        }
    ]
    assert repository.count_calls == [{"dept": None, "category": None, "q": None}]


def test_list_decided_defaults_to_decided_desc() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals?status=approved",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert repository.list_calls[0]["sort"] == "decided_desc"


def test_list_passes_filters_and_wraps_keyword() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals",
        params={
            "status": "rejected",
            "category": "market",
            "dept": "市场部",
            "q": "张三",
            "sort": "created_desc",
            "page": 2,
            "size": 50,
        },
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert repository.list_calls == [
        {
            "status": "rejected",
            "dept": "%市场部%",
            "category": "market",
            "q": "%张三%",
            "sort": "created_desc",
            "page": 2,
            "size": 50,
        }
    ]
    assert repository.count_calls == [
        {"dept": "%市场部%", "category": "market", "q": "%张三%"}
    ]


def test_detail_returns_content_and_records_single_sensitive_read() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals/9",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "活动回T退订"
    assert body["batch_status"] == "pending_approval"
    assert repository.detail_ids == [9]
    assert len(auditor.calls) == 1
    call = auditor.calls[0]
    assert call["action"] == "approval_content_read"
    assert call["object_type"] == "approval"
    assert call["object_id"] == "9"
    assert call["count"] == 1


def test_detail_unknown_id_maps_to_not_found_without_audit() -> None:
    repository = FakeApprovalRepository()
    auditor = FakeAuditor()
    response = TestClient(make_query_app(repository, auditor)).get(
        "/api/v1/web/approvals/404",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert auditor.calls == []


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
        if action == "approve":
            return replace(
                self.case,
                status="approved",
                batch_status="scheduled",
                outbox_persisted=True,
                deferred_reason="营销发送窗口外，改派定时发送",
            )
        return replace(
            self.case,
            status="rejected",
            batch_status="rejected",
            outbox_persisted=True,
        )

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


def test_decision_returns_outcome_body() -> None:
    repository = RecordingRepository()
    response = TestClient(make_app(make_real_service(repository))).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "approve"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "approved",
        "batch_status": "scheduled",
        "deferred_reason": "营销发送窗口外，改派定时发送",
    }


def test_duplicate_decision_maps_to_state_conflict() -> None:
    repository = RecordingRepository()
    repository.case = replace(repository.case, status="approved")
    response = TestClient(make_app(make_real_service(repository))).post(
        "/api/v1/web/approvals/3/decision",
        headers={"Authorization": "Bearer jwt"},
        json={"action": "approve"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


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
    assert response.json() == {
        "status": "rejected",
        "batch_status": "rejected",
        "deferred_reason": None,
    }
    assert repository.transitioned == [("reject", "内容不合规")]
