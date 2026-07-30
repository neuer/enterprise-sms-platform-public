from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.signs as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.sign_management import SignRecord


class FakeFacade:
    def __init__(self, role: str = "admin") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims("admin01", "管理员", "平台部", self.role)  # type: ignore[arg-type]


class FakeService:
    def __init__(self) -> None:
        self.created = False

    async def list_all(self) -> list[SignRecord]:
        return [SignRecord(1, "青鸾平台", "21", "approved", None)]

    async def create(self, *, name: str, actor: str) -> SignRecord:
        assert name == "新签名" and actor == "admin01"
        self.created = True
        return SignRecord(2, name, "22", "pending", None)


def client(service: FakeService, role: str = "admin") -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[api.get_sign_service] = lambda: service
    return TestClient(app)


def test_operator_plus_can_list_but_only_admin_can_create() -> None:
    service = FakeService()
    headers = {"Authorization": "Bearer jwt"}
    assert client(service, "operator").get("/api/v1/web/signs", headers=headers).status_code == 200
    forbidden = client(service, "operator").post(
        "/api/v1/web/signs", headers=headers, json={"name": "新签名"}
    )
    created = client(service).post(
        "/api/v1/web/signs", headers=headers, json={"name": "新签名"}
    )
    assert forbidden.status_code == 403
    assert created.status_code == 200
    assert created.json()["vendor_sign_id"] == "22"
    assert service.created
