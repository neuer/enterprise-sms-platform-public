from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.sensitive_words as api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.services.sensitive import SensitiveWord


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims("admin01", "管理员", "平台部", "admin")


class FakeManager:
    async def list_words(self) -> list[SensitiveWord]:
        return [SensitiveWord(1, "诈骗")]

    async def add(self, words: list[str], *, actor: str) -> list[SensitiveWord]:
        assert actor == "admin01"
        return [SensitiveWord(2, words[0])]

    async def delete(self, word_id: int, *, actor: str) -> bool:
        return word_id == 1 and actor == "admin01"


def test_sensitive_word_admin_crud() -> None:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[api.get_sensitive_word_manager] = lambda: FakeManager()
    client = TestClient(app)
    headers = {"Authorization": "Bearer jwt"}

    assert client.get("/api/v1/web/admin/sensitive-words", headers=headers).json() == [
        {"id": 1, "word": "诈骗"}
    ]
    created = client.post(
        "/api/v1/web/admin/sensitive-words",
        headers=headers,
        json={"words": ["赌博"]},
    )
    assert created.json() == [{"id": 2, "word": "赌博"}]
    assert client.delete("/api/v1/web/admin/sensitive-words/1", headers=headers).status_code == 204
