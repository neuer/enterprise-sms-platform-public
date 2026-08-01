from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.security_daily as security_daily_api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler, internal_error_handler
from app.services.security_daily import (
    SecurityDailyControlError,
    SecurityDailyOverview,
    SecurityDailyPage,
    SecurityDailyQuery,
)

NOW = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)


class FakeFacade:
    async def verify(self, token: str) -> JwtClaims:
        del token
        return JwtClaims(1, 11, "local", "admin", "管理员", "平台部", "admin")


class FakeService:
    def __init__(self, overview: SecurityDailyOverview) -> None:
        self.overview_value = overview
        self.list_error: Exception | None = None

    async def overview(self) -> SecurityDailyOverview:
        return self.overview_value

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        if self.list_error is not None:
            raise self.list_error
        return SecurityDailyPage((), 0, query.page, query.page_size)


def overview(
    *,
    configuration_state: str,
    enabled: bool,
    resend_configured: bool,
    recipient_count: int,
) -> SecurityDailyOverview:
    return SecurityDailyOverview(
        enabled=enabled,
        configuration_state=configuration_state,  # type: ignore[arg-type]
        schedule_time="08:00",
        timezone="Asia/Shanghai",
        period_description="汇总前一上海自然日",
        last_generated_at=None,
        last_delivered_at=None,
        next_scheduled_at=NOW if enabled else None,
        latest_failure=None,
        delivery_status=None,
        recipient_count=recipient_count,
        resend_configured=resend_configured,
        sender_domain="reports.neuer.cn",
        sender_address="security-daily@reports.neuer.cn",
        beat_restart_required=True,
    )


def client(service: FakeService, *, internal_handler: bool = False) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    if internal_handler:
        app.add_exception_handler(Exception, internal_error_handler)
    app.include_router(security_daily_api.router)
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade()
    app.dependency_overrides[security_daily_api.get_security_daily_service] = lambda: service
    return TestClient(app, raise_server_exceptions=not internal_handler)


@pytest.mark.parametrize(
    ("state", "enabled", "configured", "recipients", "next_run"),
    [
        ("disabled", False, False, 0, None),
        ("dispatcher_missing", True, False, 0, NOW),
        ("recipients_empty", True, True, 0, NOW),
        ("ready", True, True, 1, NOW),
    ],
)
def test_overview_exposes_explicit_configuration_state_and_no_fake_disabled_schedule(
    state: str,
    enabled: bool,
    configured: bool,
    recipients: int,
    next_run: datetime | None,
) -> None:
    http = client(
        FakeService(
            overview(
                configuration_state=state,
                enabled=enabled,
                resend_configured=configured,
                recipient_count=recipients,
            )
        )
    )

    response = http.get(
        "/api/v1/web/admin/security-daily/overview",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configuration_state"] == state
    assert (
        datetime.fromisoformat(body["next_scheduled_at"].replace("Z", "+00:00")) == next_run
        if next_run
        else body["next_scheduled_at"] is None
    )


def test_list_control_failure_is_a_unified_503_error() -> None:
    service = FakeService(
        overview(
            configuration_state="ready",
            enabled=True,
            resend_configured=True,
            recipient_count=1,
        )
    )
    service.list_error = SecurityDailyControlError("控制面不可用")

    response = client(service).get(
        "/api/v1/web/admin/security-daily/reports",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "SECURITY_DAILY_UNAVAILABLE",
        "message": "安全日报独立投递控制面不可用",
        "detail": None,
    }


def test_unexpected_list_failure_keeps_unified_500_error_without_partial_response() -> None:
    service = FakeService(
        overview(
            configuration_state="ready",
            enabled=True,
            resend_configured=True,
            recipient_count=1,
        )
    )
    service.list_error = RuntimeError("database detail must stay server-side")

    response = client(service, internal_handler=True).get(
        "/api/v1/web/admin/security-daily/reports",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert response.json()["message"] == "服务内部错误"
    assert "database detail" not in response.text
