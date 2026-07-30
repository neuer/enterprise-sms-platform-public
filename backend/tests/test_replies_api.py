from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.replies as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.reply_query import ReplyItem, ReplyNotFound, ReplyPage


class FakeFacade:
    def __init__(self, role: str) -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims("operator01", "操作员", "研发部", self.role)  # type: ignore[arg-type]


class FakeService:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.list_calls: list[dict[str, object]] = []
        self.optout_calls: list[dict[str, object]] = []

    async def list_page(self, **values: object) -> ReplyPage:
        self.list_calls.append(values)
        return ReplyPage(
            1,
            (
                ReplyItem(
                    5,
                    "138****8000",
                    "TD",
                    "BATCH-1",
                    datetime.fromisoformat("2026-07-12T08:00:00+08:00"),
                ),
            ),
        )

    async def optout(self, reply_id: int, **values: object) -> None:
        self.optout_calls.append({"reply_id": reply_id, **values})
        if self.missing:
            raise ReplyNotFound("回复不存在")


def client(service: FakeService, role: str) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[api.get_reply_service] = lambda: service
    return TestClient(app)


def test_viewer_plus_lists_only_masked_reply_fields_with_department_scope() -> None:
    service = FakeService()
    response = client(service, "viewer").get(
        "/api/v1/web/replies?phone=13800138000&page=1",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "total": 1,
        "items": [
            {
                "id": 5,
                "phone": "138****8000",
                "content": "TD",
                "batch_no": "BATCH-1",
                "reply_time": "2026-07-12T08:00:00+08:00",
            }
        ],
    }
    assert service.list_calls[0]["dept"] == "研发部"
    assert service.list_calls[0]["phone"] == "13800138000"


def test_operator_can_optout_but_viewer_is_forbidden() -> None:
    service = FakeService()
    headers = {"Authorization": "Bearer jwt"}

    assert (
        client(service, "operator")
        .post("/api/v1/web/replies/5/blacklist", headers=headers)
        .status_code
        == 200
    )
    assert service.optout_calls == [{"reply_id": 5, "dept": "研发部", "actor": "operator01"}]
    assert (
        client(FakeService(), "viewer")
        .post("/api/v1/web/replies/5/blacklist", headers=headers)
        .status_code
        == 403
    )


def test_optout_missing_reply_returns_not_found() -> None:
    response = client(FakeService(missing=True), "admin").post(
        "/api/v1/web/replies/404/blacklist",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_blacklist_endpoint_is_audited() -> None:
    assert vars(api.blacklist_reply)["__audited_action__"] == "reply_optout"
