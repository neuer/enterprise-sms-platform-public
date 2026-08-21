from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.api.apps import get_app_management_service
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.main import create_app
from app.services.callback_authority import CallbackAuthorityBusy

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
    "api_key_prefix": "sk-oa001",
    "old_key_prefix": None,
    "old_key_expires_at": None,
    "callback_secret_configured": False,
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


def test_app_responses_filter_key_material_but_expose_safe_key_metadata() -> None:
    browser = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    listed = browser.get("/api/v1/web/admin/apps", headers=headers)
    assert listed.status_code == 200
    assert "api_key_hash" not in listed.text
    assert "callback_secret_enc" not in listed.text
    assert "must-not-leak" not in listed.text
    row = listed.json()[0]
    assert row["api_key_prefix"] == "sk-oa001"
    assert row["old_key_prefix"] is None
    assert row["old_key_expires_at"] is None
    assert row["callback_secret_configured"] is False
    assert row["created_at"].startswith("2026-07-11")

    detail = browser.get("/api/v1/web/admin/apps/1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["api_key_prefix"] == "prefixxx"
    assert "api_key_hash" not in detail.text

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


def test_active_callback_delivery_blocks_secret_rotation_with_state_conflict() -> None:
    class BusyService(FakeAppService):
        async def rotate_callback_secret(
            self,
            app_id: int,
            *,
            actor: str,
            ip: str,
        ) -> dict[str, str]:
            raise CallbackAuthorityBusy("回调正在投递，请稍后重试配置变更")

    app = create_app()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[get_app_management_service] = lambda: BusyService()
    response = TestClient(app).post(
        "/api/v1/web/admin/apps/1/rotate-callback-secret",
        headers={"Authorization": "Bearer admin.jwt"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


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
