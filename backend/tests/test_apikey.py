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
from app.core.auth.principal_context import current_audit_principal
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
        current_hash_algorithm="legacy_sha256",
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
            current_hash_algorithm="legacy_sha256",
            previous_hash_algorithm="legacy_sha256",
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
        principal = current_audit_principal()
        return {
            "app_id": context.app_id,
            "audit_app_id": principal.actor_app_id if principal is not None else None,
            "categories": sorted(context.allowed_categories),
        }

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
    assert success.json() == {
        "app_id": 1,
        "audit_app_id": 1,
        "categories": ["verify"],
    }


def _probe_app(candidates: list[ApiKeyCandidate]) -> FastAPI:
    class ProbeRepository(FakeRepository):
        pass

    repository = ProbeRepository(candidates)
    authenticator = ApiKeyAuthenticator(repository)
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_api_key_authenticator] = lambda: authenticator

    @app.get("/api/v1/messages/probe")
    async def probe(
        context: Annotated[ApiAppContext, Depends(require_api_app)],
    ) -> dict[str, object]:
        return {"app_id": context.app_id, "ips": list(context.allowed_ips)}

    return app


def test_ip_allowlist_accepts_matching_cidr_and_blocks_other_sources() -> None:
    key = "current_key_with_enough_entropy_123"
    candidate = ApiKeyCandidate(
        app_id=21,
        name="app-ip",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=("203.0.113.0/24", "2001:db8::/32"),
    )
    app = _probe_app([candidate])
    headers = {"X-Api-Key": key}

    allowed_v4 = TestClient(app, client=("203.0.113.9", 12345))
    assert allowed_v4.get("/api/v1/messages/probe", headers=headers).status_code == 200

    allowed_v6 = TestClient(app, client=("2001:db8::1", 12345))
    assert allowed_v6.get("/api/v1/messages/probe", headers=headers).status_code == 200

    blocked = TestClient(app, client=("198.51.100.9", 12345))
    response = blocked.get("/api/v1/messages/probe", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "IP_NOT_ALLOWED"


def test_production_empty_allowlist_requires_unexpired_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProductionSettings:
        environment = "production"

    monkeypatch.setattr("app.core.apikey.get_settings", lambda: ProductionSettings())
    key = "current_key_with_enough_entropy_123"
    expired = ApiKeyCandidate(
        app_id=22,
        name="app-open",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=(),
    )
    blocked = TestClient(_probe_app([expired]), client=("192.0.2.7", 12345)).get(
        "/api/v1/messages/probe",
        headers={"X-Api-Key": key},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "IP_NOT_ALLOWED"

    exempt = ApiKeyCandidate(
        app_id=22,
        name="app-open",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=(),
        ip_allowlist_exempt_until=datetime(2099, 1, 1, tzinfo=UTC),
    )
    allowed = TestClient(_probe_app([exempt]), client=("192.0.2.7", 12345)).get(
        "/api/v1/messages/probe",
        headers={"X-Api-Key": key},
    )
    assert allowed.status_code == 200


def test_empty_allowlist_allows_any_source_and_unparsable_client_fails_closed() -> None:
    key = "current_key_with_enough_entropy_123"
    unrestricted = ApiKeyCandidate(
        app_id=22,
        name="app-open",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=(),
    )
    assert (
        TestClient(_probe_app([unrestricted]), client=("192.0.2.7", 12345))
        .get("/api/v1/messages/probe", headers={"X-Api-Key": key})
        .status_code
        == 200
    )

    restricted = ApiKeyCandidate(
        app_id=23,
        name="app-restricted",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=("10.0.0.0/8",),
    )
    # TestClient 默认 client host 为 "testclient"，不是合法 IP → fail closed。
    response = TestClient(_probe_app([restricted])).get(
        "/api/v1/messages/probe",
        headers={"X-Api-Key": key},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "IP_NOT_ALLOWED"


def test_corrupt_allowlist_entry_fails_closed_without_plaintext_detail() -> None:
    key = "current_key_with_enough_entropy_123"
    candidate = ApiKeyCandidate(
        app_id=24,
        name="app-corrupt",
        dept="平台部",
        allowed_categories="notice",
        current_hash=digest(key),
        previous_hash=None,
        previous_expires_at=None,
        current_hash_algorithm="legacy_sha256",
        allowed_ips=("not-a-cidr",),
    )
    response = TestClient(_probe_app([candidate]), client=("10.1.2.3", 12345)).get(
        "/api/v1/messages/probe",
        headers={"X-Api-Key": key},
    )
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "not-a-cidr" not in response.text
