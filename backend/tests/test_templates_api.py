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
        return JwtClaims(
            1,
            11,
            "local",
            "operator01",
            "操作员",
            "平台部",
            "operator",
        )


class FakeService:
    def __init__(
        self,
        *,
        vendor_state: str = "pending",
        vendor_template_id: str | None = "21",
    ) -> None:
        self.created: dict[str, object] | None = None
        self.vendor_state = vendor_state
        self.vendor_template_id = vendor_template_id
        self.updated: list[int] = []
        self.deleted: list[int] = []

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        assert dept is None
        return []

    async def create(self, **values: object) -> TemplateRecord:
        self.created = values
        return TemplateRecord(
            1,
            "验证码",
            "验证码{1}",
            [{"pos": 1, "max_len": 6}],
            "",
            None,
            "pending",
            None,
        )

    async def get(self, template_id: int, *, dept: str | None) -> TemplateRecord:
        assert template_id == 1 and dept is None
        return TemplateRecord(
            1,
            "验证码",
            "验证码{1}",
            [{"pos": 1, "max_len": 6}],
            "其他业务部",
            self.vendor_template_id,
            self.vendor_state,
            None,
        )

    async def update(self, template_id: int, **values: object) -> TemplateRecord:
        self.updated.append(template_id)
        return TemplateRecord(
            template_id,
            str(values["name"]),
            str(values["content"]),
            values["var_specs"],  # type: ignore[arg-type]
            "其他业务部",
            None,
            "pending",
            None,
        )

    async def delete(self, template_id: int, *, actor: str) -> None:
        assert actor == "operator01"
        self.deleted.append(template_id)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.principals: list[str] = []

    async def send_template(self, template_id: int, **values: object) -> None:
        self.sent.append(template_id)
        self.principals.append(values["principal"].login_name)  # type: ignore[attr-defined]


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
    assert service.created is not None
    assert service.created["dept"] == ""
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
    assert sender.principals == ["operator01"]


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


def test_manual_template_sync_allows_approved_template_with_vendor_id() -> None:
    service = FakeService(vendor_state="approved")
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


@pytest.mark.parametrize("vendor_state", ["draft"])
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


def test_operator_can_update_and_delete_template_from_another_department() -> None:
    service = FakeService(vendor_state="draft", vendor_template_id=None)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_template_service] = lambda: service
    client = TestClient(app)
    headers = {"Authorization": "Bearer jwt"}

    updated = client.put(
        "/api/v1/web/templates/1",
        headers=headers,
        json={
            "name": "跨部门通知",
            "content": "通知{1}",
            "var_specs": [{"pos": 1, "max_len": 10}],
        },
    )
    removed = client.delete("/api/v1/web/templates/1", headers=headers)

    assert updated.status_code == 200
    assert updated.json()["dept"] == "其他业务部"
    assert removed.status_code == 204
    assert service.updated == [1]
    assert service.deleted == [1]
