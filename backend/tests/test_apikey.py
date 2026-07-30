from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.apikey import (
    ApiAppContext,
    ApiKeyAuthenticator,
    ApiKeyCandidate,
    InvalidApiKey,
    get_api_key_authenticator,
    require_api_app,
)
from app.core.errors import ApiError, api_error_handler


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeRepository:
    def __init__(self, candidates: list[ApiKeyCandidate]) -> None:
        self.candidates = candidates
        self.prefixes: list[str] = []

    async def find_candidates(self, prefix: str) -> list[ApiKeyCandidate]:
        self.prefixes.append(prefix)
        return self.candidates


@pytest.mark.asyncio
async def test_authenticator_accepts_current_key_and_injects_categories() -> None:
    key = "current_key_with_enough_entropy_123"
    candidate = ApiKeyCandidate(
        app_id=7,
        name="app-oa",
        dept="业务一部",
        allowed_categories="notice,market",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
    )
    repository = FakeRepository([candidate])
    auth = ApiKeyAuthenticator(repository)

    context = await auth.authenticate(key)

    assert repository.prefixes == [key[:8]]
    assert context == ApiAppContext(7, "app-oa", "业务一部", frozenset({"notice", "market"}))


@pytest.mark.asyncio
async def test_previous_key_only_works_during_grace_period() -> None:
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    previous = "previous_key_with_enough_entropy"

    def candidate(expires: datetime) -> ApiKeyCandidate:
        return ApiKeyCandidate(
            8,
            "app-old",
            "研发部",
            "verify",
            digest("different-current-key"),
            digest(previous),
            expires,
        )

    valid = ApiKeyAuthenticator(
        FakeRepository([candidate(now + timedelta(seconds=1))]),
        clock=lambda: now,
    )
    assert (await valid.authenticate(previous)).app_id == 8

    expired = ApiKeyAuthenticator(
        FakeRepository([candidate(now)]),
        clock=lambda: now,
    )
    with pytest.raises(InvalidApiKey):
        await expired.authenticate(previous)


@pytest.mark.asyncio
async def test_wrong_or_short_key_is_rejected_without_identity_leak() -> None:
    repository = FakeRepository([])
    auth = ApiKeyAuthenticator(repository)
    with pytest.raises(InvalidApiKey, match="API Key 无效"):
        await auth.authenticate("short")
    assert repository.prefixes == []


def test_explicit_dependency_protects_route_and_returns_app_context() -> None:
    class FakeAuthenticator:
        async def authenticate(self, key: str) -> ApiAppContext:
            if key != "valid-key-value":
                raise InvalidApiKey("API Key 无效")
            return ApiAppContext(1, "app", "研发部", frozenset({"verify"}))

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_api_key_authenticator] = FakeAuthenticator

    @app.get("/api/v1/messages/probe")
    async def probe(
        context: Annotated[ApiAppContext, Depends(require_api_app)],
    ) -> dict[str, object]:
        return {"app_id": context.app_id, "categories": sorted(context.allowed_categories)}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    missing = client.get("/api/v1/messages/probe")
    assert missing.status_code == 401
    assert missing.json()["code"] == "UNAUTHORIZED"
    success = client.get(
        "/api/v1/messages/probe",
        headers={"X-Api-Key": "valid-key-value"},
    )
    assert success.json() == {"app_id": 1, "categories": ["verify"]}
