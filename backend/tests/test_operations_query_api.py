from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api import web_messages as module
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler, validation_error_handler
from app.services.batch_query import BatchAccessScope
from app.services.operations_query import (
    MessageQueryItem,
    MessageQueryPage,
    PhoneBadge,
    Timeline,
    TimelineEvent,
)


class FakeFacade:
    def __init__(self, role: str = "viewer") -> None:
        self.role = role

    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims("tester", "测试用户", "平台部", self.role)  # type: ignore[arg-type]


class FakeBatchQueries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_batches(self, **values: object) -> dict[str, object]:
        self.calls.append(values)
        return {"total": 0, "items": []}


class FakeOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search_messages(self, **values: object) -> MessageQueryPage:
        self.calls.append(("search", values))
        return MessageQueryPage(
            1,
            (
                MessageQueryItem(
                    9,
                    "138****8000",
                    "delivered",
                    "DELIVRD",
                    datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
                    datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                    "BATCH-1",
                    "notice",
                    "系统通知",
                    "通知应用",
                ),
            ),
        )

    async def timeline(self, **values: object) -> Timeline:
        self.calls.append(("timeline", values))
        return Timeline(
            PhoneBadge(True, "reply_optout", 3),
            (
                TimelineEvent(
                    datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                    "in",
                    "notice",
                    "BATCH-1",
                    "退订",
                    None,
                    "用户",
                ),
            ),
        )

    async def decrypt_phone(self, _id: int, **values: object) -> str:
        self.calls.append(("decrypt", values))
        return "13800138000"


def make_client(
    *,
    role: str = "viewer",
) -> tuple[TestClient, FakeBatchQueries, FakeOperations]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    batches = FakeBatchQueries()
    operations = FakeOperations()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[module.get_batch_query_service] = lambda: batches
    app.dependency_overrides[module.get_operations_query_service] = lambda: operations
    app.include_router(module.router)
    return TestClient(app), batches, operations


def test_viewer_batch_filters_are_locked_to_jwt_department() -> None:
    client, batches, _ = make_client()
    response = client.get(
        "/api/v1/web/batches?category=notice&is_test=false&page=2",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    assert batches.calls[0]["scope"] == BatchAccessScope(dept="平台部")
    assert batches.calls[0]["category"] == "notice"
    assert batches.calls[0]["is_test"] is False
    assert batches.calls[0]["page"] == 2


def test_non_admin_cannot_request_another_department() -> None:
    client, _, _ = make_client(role="operator")
    response = client.get(
        "/api/v1/web/batches?dept=财务部",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_message_search_and_timeline_return_masked_contract() -> None:
    client, _, operations = make_client(role="approver")
    headers = {"Authorization": "Bearer jwt"}
    search = client.get("/api/v1/web/messages?phone=13800138000", headers=headers)
    timeline = client.get(
        "/api/v1/web/messages/timeline?phone=13800138000",
        headers=headers,
    )

    assert search.status_code == 200
    assert search.json()["items"][0]["phone"] == "138****8000"
    assert "phone_hmac" not in search.text and "phone_enc" not in search.text
    assert timeline.status_code == 200
    assert timeline.json()["badge"] == {
        "blacklisted": True,
        "blacklist_source": "reply_optout",
        "recv_30d": 3,
    }
    assert operations.calls[0][1]["scope"] == BatchAccessScope(dept="平台部")


def test_decrypt_requires_approver_or_admin_and_passes_actor_scope() -> None:
    viewer, _, _ = make_client(role="viewer")
    denied = viewer.post(
        "/api/v1/web/messages/9/phone/decrypt",
        headers={"Authorization": "Bearer jwt"},
    )
    assert denied.status_code == 403

    approver, _, operations = make_client(role="approver")
    allowed = approver.post(
        "/api/v1/web/messages/9/phone/decrypt",
        headers={"Authorization": "Bearer jwt"},
    )
    assert allowed.status_code == 200 and allowed.json() == {"phone": "13800138000"}
    assert allowed.headers["cache-control"] == "no-store"
    assert operations.calls[-1] == (
        "decrypt",
        {"scope": BatchAccessScope(dept="平台部"), "actor": "tester"},
    )
    assert module.decrypt_message_phone.__audited_action__ == "message_phone_decrypt"  # type: ignore[attr-defined]
