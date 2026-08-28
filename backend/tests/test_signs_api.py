from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.signs as api
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.sign_management import SignRecord


class FakeFacade:
    def __init__(self, role: str = "admin") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(
            1,
            11,
            "local",
            "admin01",
            "管理员",
            "平台部",
            self.role,  # type: ignore[arg-type]
        )


class FakeService:
    def __init__(
        self,
        *,
        vendor_state: str = "pending",
        vendor_sign_id: str | None = "21",
    ) -> None:
        self.created = False
        self.vendor_state = vendor_state
        self.vendor_sign_id = vendor_sign_id

    async def list_all(self) -> list[SignRecord]:
        return [SignRecord(1, "青鸾平台", "21", "approved", None)]

    async def create(self, *, name: str, actor: str) -> SignRecord:
        assert name == "新签名" and actor == "admin01"
        self.created = True
        return SignRecord(2, name, None, "pending", None)

    async def get(self, sign_id: int) -> SignRecord:
        assert sign_id == 1
        return SignRecord(
            1,
            "青鸾平台",
            self.vendor_sign_id,
            self.vendor_state,
            None,
        )

    async def prepare_adoption(
        self,
        sign_id: int,
        *,
        confirmed_name: str,
    ) -> SignRecord:
        assert sign_id == 1
        assert confirmed_name == "青鸾平台"
        return SignRecord(1, "青鸾平台", None, "pending", None)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.principals: list[str] = []

    async def send_sign(self, sign_id: int, **values: object) -> None:
        self.sent.append(sign_id)
        self.principals.append(values["principal"].login_name)  # type: ignore[attr-defined]


class FakeAdoptionSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str, str]] = []

    async def send_sign(
        self,
        sign_id: int,
        vendor_sign_id: int,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> None:
        self.sent.append((sign_id, vendor_sign_id, principal.login_name, ip))


def sender_dependency(sender: FakeSender) -> Callable[[], FakeSender]:
    return lambda: sender


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
    assert created.json()["vendor_sign_id"] is None
    assert service.created


def test_manual_sign_sync_enqueues_exact_authorized_sign() -> None:
    service = FakeService()
    sender = FakeSender()
    app_client = client(service)
    app_client.app.dependency_overrides[api.get_sign_job_sender] = lambda: sender

    response = app_client.post(
        "/api/v1/web/signs/1/sync",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 202
    assert sender.sent == [1]
    assert sender.principals == ["admin01"]


def test_manual_sign_sync_allows_approved_and_rejected_bound_states() -> None:
    headers = {"Authorization": "Bearer jwt"}
    for state in ("approved", "rejected"):
        service = FakeService(vendor_state=state)
        sender = FakeSender()
        app_client = client(service)
        app_client.app.dependency_overrides[api.get_sign_job_sender] = sender_dependency(sender)

        response = app_client.post("/api/v1/web/signs/1/sync", headers=headers)

        assert response.status_code == 202
        assert sender.sent == [1]


def test_manual_sign_sync_rejects_unbound_sign() -> None:
    service = FakeService(vendor_sign_id=None)
    sender = FakeSender()
    app_client = client(service)
    app_client.app.dependency_overrides[api.get_sign_job_sender] = lambda: sender

    response = app_client.post(
        "/api/v1/web/signs/1/sync",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 409
    assert sender.sent == []


def test_admin_can_enqueue_exact_existing_sign_adoption() -> None:
    service = FakeService()
    sender = FakeAdoptionSender()
    app_client = client(service)
    app_client.app.dependency_overrides[api.get_sign_adoption_sender] = lambda: sender

    response = app_client.post(
        "/api/v1/web/signs/1/adopt-existing",
        headers={"Authorization": "Bearer jwt"},
        json={"vendor_sign_id": 112074, "confirmed_name": "青鸾平台"},
    )

    assert response.status_code == 202
    assert sender.sent == [(1, 112074, "admin01", "0.0.0.0")]


def test_non_admin_cannot_adopt_existing_sign() -> None:
    service = FakeService()
    sender = FakeAdoptionSender()
    app_client = client(service, "operator")
    app_client.app.dependency_overrides[api.get_sign_adoption_sender] = lambda: sender

    response = app_client.post(
        "/api/v1/web/signs/1/adopt-existing",
        headers={"Authorization": "Bearer jwt"},
        json={"vendor_sign_id": 112074, "confirmed_name": "青鸾平台"},
    )

    assert response.status_code == 403
    assert sender.sent == []


def test_adoption_persistence_failure_is_a_structured_503() -> None:
    service = FakeService()

    class FailingSender(FakeAdoptionSender):
        async def send_sign(self, *args: object, **kwargs: object) -> None:
            raise api.SignAdoptionUnavailable("database unavailable")

    app_client = client(service)
    app_client.app.dependency_overrides[api.get_sign_adoption_sender] = FailingSender

    response = app_client.post(
        "/api/v1/web/signs/1/adopt-existing",
        headers={"Authorization": "Bearer jwt"},
        json={"vendor_sign_id": 112074, "confirmed_name": "青鸾平台"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"


def test_adoption_race_is_a_structured_409() -> None:
    service = FakeService()

    class RacingSender(FakeAdoptionSender):
        async def send_sign(self, *args: object, **kwargs: object) -> None:
            raise api.SignAdoptionConflict("签名自动提交正在执行，请稍后刷新")

    app_client = client(service)
    app_client.app.dependency_overrides[api.get_sign_adoption_sender] = RacingSender

    response = app_client.post(
        "/api/v1/web/signs/1/adopt-existing",
        headers={"Authorization": "Bearer jwt"},
        json={"vendor_sign_id": 112074, "confirmed_name": "青鸾平台"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
