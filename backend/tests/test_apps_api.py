from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.api.apps import get_app_management_service
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.main import create_app

APP = {
    "id": 1,
    "name": "app-oa",
    "dept": "业务一部",
    "allowed_categories": ["notice"],
    "default_sign": None,
    "daily_quota": 0,
    "rate_limit_per_min": 60,
    "blacklist_check": True,
    "freq_override": None,
    "allowed_ips": [],
    "callback_url": None,
    "callback_report_enabled": False,
    "status": 1,
    "created_at": datetime(2026, 7, 11, tzinfo=UTC),
    "updated_at": datetime(2026, 7, 11, tzinfo=UTC),
}


class FakeFacade:
    def __init__(self, role: str = "admin") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims("admin01", "管理员", "平台部", self.role)  # type: ignore[arg-type]


class FakeAppService:
    async def list(self) -> list[dict[str, Any]]:
        return [APP | {"api_key_hash": "must-not-leak", "callback_secret_enc": b"x"}]

    async def get(self, app_id: int) -> dict[str, Any]:
        return APP | {"api_key_prefix": "prefixxx"}

    async def create(self, config: Any, *, actor: str, ip: str) -> dict[str, Any]:
        return {"id": 2, "api_key": "one-time-key", "callback_secret": None}

    async def rotate_key(self, app_id: int, *, actor: str, ip: str) -> dict[str, Any]:
        return {
            "api_key": "rotated-one-time-key",
            "old_key_expires_at": datetime(2026, 7, 12, tzinfo=UTC),
        }

    async def rotate_callback_secret(
        self,
        app_id: int,
        *,
        actor: str,
        ip: str,
    ) -> dict[str, str]:
        return {"callback_secret": "rotated-callback-secret"}


def client(role: str = "admin") -> TestClient:
    app = create_app()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[get_app_management_service] = lambda: FakeAppService()
    return TestClient(app)


def test_app_responses_filter_all_key_material_and_create_returns_once() -> None:
    browser = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    listed = browser.get("/api/v1/web/admin/apps", headers=headers)
    assert listed.status_code == 200
    assert "api_key_hash" not in listed.text
    assert "callback_secret_enc" not in listed.text

    detail = browser.get("/api/v1/web/admin/apps/1", headers=headers)
    assert detail.status_code == 200
    assert "api_key_prefix" not in detail.text

    created = browser.post(
        "/api/v1/web/admin/apps",
        headers=headers,
        json={"name": "new", "dept": "研发部"},
    )
    assert created.json() == {
        "id": 2,
        "api_key": "one-time-key",
        "callback_secret": None,
    }
    assert created.headers["cache-control"] == "no-store"

    rotated_key = browser.post(
        "/api/v1/web/admin/apps/1/rotate-key",
        headers=headers,
    )
    assert rotated_key.status_code == 200
    assert rotated_key.headers["cache-control"] == "no-store"

    rotated_callback = browser.post(
        "/api/v1/web/admin/apps/1/rotate-callback-secret",
        headers=headers,
    )
    assert rotated_callback.status_code == 200
    assert rotated_callback.headers["cache-control"] == "no-store"


def test_non_admin_cannot_manage_apps() -> None:
    response = client("viewer").get(
        "/api/v1/web/admin/apps",
        headers={"Authorization": "Bearer viewer.jwt"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_frequency_override_rejects_unknown_or_non_positive_values() -> None:
    browser = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    invalid_overrides = [
        {"verify_minute": 2},
        {"verify_per_minute": True},
        {"verify_per_minute": 0},
        {"verify_per_minute": 1.5},
    ]
    for freq_override in invalid_overrides:
        response = browser.post(
            "/api/v1/web/admin/apps",
            headers=headers,
            json={"name": "new", "dept": "研发部", "freq_override": freq_override},
        )
        assert response.status_code == 400, freq_override
        assert response.json()["code"] == "INVALID_PARAM"


def test_frequency_override_accepts_only_documented_positive_integer_keys() -> None:
    response = client().post(
        "/api/v1/web/admin/apps",
        headers={"Authorization": "Bearer admin.jwt"},
        json={
            "name": "new",
            "dept": "研发部",
            "freq_override": {
                "verify_per_minute": 2,
                "verify_per_day": 20,
                "market_per_day": 1,
            },
        },
    )

    assert response.status_code == 200


def test_app_responses_echo_allowlist_and_create_rejects_invalid_entries() -> None:
    browser = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    detail = browser.get("/api/v1/web/admin/apps/1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["allowed_ips"] == []

    invalid_payloads = [
        {"allowed_ips": ["x" * 65]},
        {"allowed_ips": [123]},
        {"allowed_ips": ["10.0.0.1"] * 51},
    ]
    for invalid in invalid_payloads:
        response = browser.post(
            "/api/v1/web/admin/apps",
            headers=headers,
            json={"name": "new", "dept": "研发部", **invalid},
        )
        assert response.status_code == 400, invalid
        assert response.json()["code"] == "INVALID_PARAM"
