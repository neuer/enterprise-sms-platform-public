from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.templates as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.template_management import TemplateRecord


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims("operator01", "操作员", "平台部", "operator")


class FakeService:
    def __init__(
        self,
        *,
        vendor_state: str = "pending",
        vendor_template_id: str | None = "21",
    ) -> None:
        self.created = False
        self.vendor_state = vendor_state
        self.vendor_template_id = vendor_template_id

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
            self.vendor_template_id,
            self.vendor_state,
            None,
        )


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_template(self, template_id: int) -> None:
        self.sent.append(template_id)


class FakeAuditor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.calls.append(values)


def test_operator_can_list_and_create_template() -> None:
    service = FakeService()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    auditor = FakeAuditor()
    app.dependency_overrides[api.get_sensitive_read_auditor] = lambda: auditor
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
    assert auditor.calls[0]["action"] == "template_content_read"
    assert auditor.calls[0]["count"] == 0


def test_manual_template_sync_enqueues_only_the_authorized_template_id() -> None:
    service = FakeService()
    sender = FakeSender()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
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


def test_manual_template_sync_allows_rejected_template_with_vendor_id() -> None:
    service = FakeService(vendor_state="rejected")
    sender = FakeSender()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
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


def test_manual_template_sync_rejects_template_without_vendor_id() -> None:
    service = FakeService(vendor_template_id=None)
    sender = FakeSender()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    app.dependency_overrides[api.get_template_job_sender] = lambda: sender

    response = TestClient(app).post(
        "/api/v1/web/templates/1/sync",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 409
    assert sender.sent == []


@pytest.mark.parametrize("vendor_state", ["approved", "draft"])
def test_manual_template_sync_rejects_non_syncable_state(vendor_state: str) -> None:
    service = FakeService(vendor_state=vendor_state)
    sender = FakeSender()
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    app.dependency_overrides[api.get_template_job_sender] = lambda: sender

    response = TestClient(app).post(
        "/api/v1/web/templates/1/sync",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 409
    assert sender.sent == []


def test_template_detail_records_sensitive_read_audit() -> None:
    service = FakeService()
    auditor = FakeAuditor()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    app.dependency_overrides[api.get_sensitive_read_auditor] = lambda: auditor

    response = TestClient(app).get(
        "/api/v1/web/templates/1",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    assert auditor.calls[0]["action"] == "template_content_read"
    assert auditor.calls[0]["object_id"] == "1"
