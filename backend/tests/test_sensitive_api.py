from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.sensitive_words as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.services.sensitive import SensitiveWord, SensitiveWordAddResult, SensitiveWordPage


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims("admin01", "管理员", "平台部", "admin")


class FakeManager:
    def __init__(self) -> None:
        self.last_filters: dict[str, object] = {}

    async def list_page(self, **filters: object) -> SensitiveWordPage:
        self.last_filters = filters
        return SensitiveWordPage(total=1, items=[SensitiveWord(1, "诈骗")])

    async def add(self, words: list[str], *, actor: str) -> SensitiveWordAddResult:
        assert actor == "admin01"
        return SensitiveWordAddResult([SensitiveWord(2, words[0])], skipped=1)

    async def delete(self, word_id: int, *, actor: str) -> bool:
        return word_id == 1 and actor == "admin01"


def test_sensitive_word_admin_crud() -> None:
    manager = FakeManager()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_sensitive_word_manager] = lambda: manager
    client = TestClient(app)
    headers = {"Authorization": "Bearer jwt"}

    listed = client.get(
        "/api/v1/web/admin/sensitive-words?keyword=诈&page=2&size=50",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["word"] == "诈骗"
    assert manager.last_filters == {"keyword": "诈", "page": 2, "size": 50}

    created = client.post(
        "/api/v1/web/admin/sensitive-words",
        headers=headers,
        json={"words": ["赌博"]},
    )
    assert created.status_code == 200
    assert created.json()["added"] == 1
    assert created.json()["skipped"] == 1
    assert created.json()["items"] == [{"id": 2, "word": "赌博", "created_at": None}]

    assert client.delete("/api/v1/web/admin/sensitive-words/1", headers=headers).status_code == 204
