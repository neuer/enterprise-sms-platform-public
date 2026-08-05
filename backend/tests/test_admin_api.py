from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin as admin_api
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.roles import Role
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.admin import AuditQuery, AuditRecord, ConfigItem, ConfigUpdate

NOW = datetime(2026, 7, 12, 8, tzinfo=UTC)
CORRELATION_ID = UUID("30000000-0000-4000-8000-000000000009")


class FakeFacade:
    def __init__(self, role: Role = "admin") -> None:
        self.role: Role = role

    async def verify(self, token: str) -> JwtClaims:
        return JwtClaims(1, 10, "local", "admin01", "管理员", "平台部", self.role)


class FakeService:
    def __init__(self) -> None:
        self.updates: list[tuple[tuple[ConfigUpdate, ...], SecurityPrincipal, str]] = []

    async def list_audits(self, query: AuditQuery) -> tuple[tuple[AuditRecord, ...], int]:
        return (
            (
                AuditRecord(
                    1,
                    "admin01",
                    "admin",
                    "10.0.0.8",
                    "config_update",
                    "sys_config",
                    "vendor_qps",
                    {"value": "5"},
                    {"value": "8"},
                    NOW,
                    correlation_id=CORRELATION_ID,
                ),
            ),
            1,
        )

    async def list_configs(self) -> tuple[ConfigItem, ...]:
        return (
            ConfigItem(
                "vendor_qps",
                "5",
                "int",
                "厂商 QPS",
                "运行调度",
                False,
                True,
                False,
                None,
                NOW,
                "5",
                None,
                1_000,
            ),
        )

    async def update_configs(
        self,
        updates: tuple[ConfigUpdate, ...],
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[ConfigItem, ...]:
        self.updates.append((updates, principal, ip))
        return await self.list_configs()


def client(role: Role = "admin") -> tuple[TestClient, FakeService]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(admin_api.router)
    service = FakeService()
    app.dependency_overrides[admin_api.get_admin_service] = lambda: service
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    return TestClient(app), service


def test_admin_can_query_audits_and_update_configs() -> None:
    http, service = client()
    headers = {"Authorization": "Bearer test"}

    audits = http.get(
        "/api/v1/web/admin/audit-logs?action=config_update&page=1&page_size=20",
        headers=headers,
    )
    configs = http.get("/api/v1/web/admin/configs", headers=headers)
    updated = http.put(
        "/api/v1/web/admin/configs",
        headers=headers,
        json={"items": [{"key": "vendor_qps", "value": "8"}]},
    )

    assert audits.status_code == 200 and audits.json()["total"] == 1
    assert audits.json()["items"][0]["after_val"] == {"value": "8"}
    assert audits.json()["items"][0]["correlation_id"] == str(CORRELATION_ID)
    assert configs.status_code == 200 and configs.json()[0]["group"] == "运行调度"
    assert configs.json()[0]["default"] == "5"
    assert configs.json()[0]["min_value"] is None
    assert configs.json()[0]["max_value"] == 1_000
    assert updated.status_code == 200
    assert service.updates[0][1].login_name == "admin01"
    assert vars(admin_api.update_configs)["__audited_action__"] == "config_update"


def test_non_admin_is_denied_without_calling_service() -> None:
    http, service = client("viewer")
    headers = {"Authorization": "Bearer test"}

    response = http.get("/api/v1/web/admin/audit-logs", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert service.updates == []
