from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.templates as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.services.template_management import TemplateRecord


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims("operator01", "操作员", "平台部", "operator")


class FakeService:
    def __init__(self) -> None:
        self.created = False

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        assert dept == "平台部"
        return []

    async def create(self, **_: object) -> TemplateRecord:
        self.created = True
        return TemplateRecord(
            1,
            "验证码",
            "验证码{1}",
            [{"pos": 1, "max_len": 6}],
            "平台部",
            None,
            "pending",
            None,
        )

    async def get(self, template_id: int, *, dept: str | None) -> TemplateRecord:
        assert template_id == 1 and dept == "平台部"
        return TemplateRecord(
            1,
            "验证码",
            "验证码{1}",
            [{"pos": 1, "max_len": 6}],
            "平台部",
            None,
            "pending",
            None,
        )


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_template(self, template_id: int) -> None:
        self.sent.append(template_id)


def test_operator_can_list_and_create_template() -> None:
    service = FakeService()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    client = TestClient(app)
    headers = {"Authorization": "Bearer jwt"}
    assert client.get("/api/v1/web/templates", headers=headers).status_code == 200
    created = client.post(
        "/api/v1/web/templates",
        headers=headers,
        json={
            "name": "验证码",
            "content": "验证码{1}",
            "var_specs": [{"pos": 1, "max_len": 6}],
        },
    )
    assert created.status_code == 200
    assert created.json()["vendor_state"] == "pending"
    assert service.created


def test_manual_template_sync_enqueues_only_the_authorized_template_id() -> None:
    service = FakeService()
    sender = FakeSender()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    app.dependency_overrides[api.get_template_job_sender] = lambda: sender

    response = TestClient(app).post(
        "/api/v1/web/templates/1/sync",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 202
    assert sender.sent == [1]
