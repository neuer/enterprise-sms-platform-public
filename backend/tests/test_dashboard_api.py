from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports as module
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.dashboard import (
    AlertSummary,
    BalancePoint,
    CategoryMetric,
    DashboardOperations,
    DashboardSnapshot,
    JobHealth,
)


class FakeFacade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims("viewer01", "只读用户", "业务一部", "viewer")


class AdminFacade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims("admin01", "管理员", "平台部", "admin")


class FakeDashboardService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get(self, *, role: str, dept: str) -> DashboardSnapshot:
        self.calls.append((role, dept))
        now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
        snapshot = DashboardSnapshot(
            now,
            (CategoryMetric("notice", 10, 12, 8, 2, 0, 0.8),),
            0.8,
            2,
        )
        if role != "admin":
            return snapshot
        return DashboardSnapshot(
            snapshot.refreshed_at,
            snapshot.categories,
            snapshot.overall_success_rate,
            snapshot.pending_approvals,
            snapshot.ui_policy,
            DashboardOperations(
                current_balance=9000,
                balances=(BalancePoint(date(2026, 7, 12), 9000),),
                alerts=(AlertSummary("warn", "余额较低", now),),
                uncertain=1,
                unmatched=3,
                callback_dead=4,
                jobs=(JobHealth("poll_report", now, "success", False),),
            ),
        )


def make_client(
    facade: type[FakeFacade] | type[AdminFacade] = FakeFacade,
) -> tuple[TestClient, FakeDashboardService]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    service = FakeDashboardService()
    app.dependency_overrides[get_auth_facade] = facade
    app.dependency_overrides[module.get_dashboard_service] = lambda: service
    app.include_router(module.router)
    return TestClient(app), service


def test_viewer_dashboard_omits_global_operational_snapshot() -> None:
    client, service = make_client()
    response = client.get(
        "/api/v1/web/reports/dashboard",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["categories"][0]["success_rate"] == 0.8
    assert body["overall_success_rate"] == 0.8
    assert body["ui_policy"] == {"test_send_max": 5}
    assert "operations" not in body
    for forbidden in (
        "current_balance",
        "balances",
        "alerts",
        "dispositions",
        "jobs",
        "channel_monitor",
    ):
        assert forbidden not in body
    assert service.calls == [("viewer", "业务一部")]


def test_admin_dashboard_includes_operational_snapshot() -> None:
    client, service = make_client(AdminFacade)

    response = client.get(
        "/api/v1/web/reports/dashboard",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    operations = response.json()["operations"]
    assert operations["current_balance"] == 9000
    assert operations["dispositions"] == {
        "uncertain": 1,
        "unmatched": 3,
        "callback_dead": 4,
    }
    assert operations["channel_monitor"]["stale"] is True
    assert service.calls == [("admin", "平台部")]


def test_dashboard_requires_bearer_token() -> None:
    client, _ = make_client()
    response = client.get("/api/v1/web/reports/dashboard")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
