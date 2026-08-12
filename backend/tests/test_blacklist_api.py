from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.blacklist as blacklist_api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.services.blacklist import BlacklistAddResult, BlacklistEntry, BlacklistPage


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(1, 11, "local", "admin01", "管理员", "平台部", "admin")


class FakeService:
    def __init__(self) -> None:
        self.entry = BlacklistEntry(
            "a" * 64,
            b"ciphertext",
            "138****8000",
            1,
            "manual",
            "投诉",
        )
        self.last_filters: dict[str, object] = {}

    async def list_page(self, **filters: object) -> BlacklistPage:
        self.last_filters = filters
        return BlacklistPage(total=1, items=[self.entry])

    async def add(self, phones: list[str], **_: object) -> BlacklistAddResult:
        assert phones == ["13800138000"]
        return BlacklistAddResult([self.entry], added=1, updated=0)

    async def delete(self, phone_hmac: str, **_: object) -> bool:
        return phone_hmac == self.entry.phone_hmac


def test_blacklist_admin_crud_never_returns_plaintext_or_ciphertext() -> None:
    service = FakeService()
    app = FastAPI()
    app.include_router(blacklist_api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[blacklist_api.get_blacklist_service] = lambda: service
    client = TestClient(app)
    headers = {"Authorization": "Bearer jwt"}

    created = client.post(
        "/api/v1/web/admin/blacklist",
        headers=headers,
        json={"phones": ["13800138000"], "source": "manual", "remark": "投诉"},
    )
    listed = client.get(
        "/api/v1/web/admin/blacklist?source=manual&keyword=投诉&page=2&size=50",
        headers=headers,
    )
    deleted = client.delete(
        f"/api/v1/web/admin/blacklist/{'a' * 64}",
        headers=headers,
    )

    assert created.status_code == 200
    assert created.json()["added"] == 1
    assert created.json()["updated"] == 0
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["phone_mask"] == "138****8000"
    assert service.last_filters == {"source": "manual", "keyword": "投诉", "page": 2, "size": 50}
    assert deleted.status_code == 204
    assert "13800138000" not in created.text + listed.text
    assert "ciphertext" not in created.text + listed.text
