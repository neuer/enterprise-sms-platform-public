from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports as module
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from app.services.reporting import (
    ReportFilterNotReady,
    ReportingDimSummary,
    ReportingResult,
    ReportingRow,
    ReportingSummary,
)


class FakeFacade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims("viewer01", "只读用户", "业务一部", "viewer")


class FakeReportingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def get(self, **kwargs: object) -> ReportingResult:
        self.calls.append(kwargs)
        return ReportingResult(
            "week",
            "app",
            "notice",
            date(2026, 7, 1),
            date(2026, 7, 12),
            False,
            ReportingSummary(10, 12, 8, 2, 1, 0.8),
            (ReportingDimSummary("7", "OA应用", 10, 12, 8, 2, 1, 0.8),),
            (ReportingRow(date(2026, 7, 6), "7", "OA应用", 10, 12, 8, 2, 1, 0.8),),
        )


def make_client() -> tuple[TestClient, FakeReportingService]:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    service = FakeReportingService()
    app.dependency_overrides[get_auth_facade] = FakeFacade
    app.dependency_overrides[module.get_reporting_service] = lambda: service
    app.include_router(module.router)
    return TestClient(app), service


def test_stats_endpoint_passes_typed_filters_and_returns_server_rate() -> None:
    client, service = make_client()
    response = client.get(
        "/api/v1/web/reports/stats",
        params={
            "granularity": "week",
            "group_by": "app",
            "category": "notice",
            "start": "2026-07-01",
            "end": "2026-07-12",
        },
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["success_rate"] == 0.8
    assert body["items"][0]["total_segments"] == 12
    assert body["summary"]["success_rate"] == 0.8
    assert body["dim_summary"] == [
        {
            "dim_value": "7",
            "dim_label": "OA应用",
            "total": 10,
            "total_segments": 12,
            "delivered": 8,
            "failed": 2,
            "unknown": 1,
            "success_rate": 0.8,
            "is_other": False,
        }
    ]
    assert body["can_export_decrypted"] is False
    assert service.calls[0]["role"] == "viewer"
    assert service.calls[0]["dept"] == "业务一部"
    assert service.calls[0]["start"] == date(2026, 7, 1)
    assert service.calls[0]["is_test"] is None


def test_stats_endpoint_keeps_false_and_maps_blank_to_all() -> None:
    client, service = make_client()
    false_response = client.get(
        "/api/v1/web/reports/stats",
        params={"is_test": "false", "start": "2026-07-01", "end": "2026-07-02"},
        headers={"Authorization": "Bearer jwt"},
    )
    true_response = client.get(
        "/api/v1/web/reports/stats",
        params={"is_test": "true"},
        headers={"Authorization": "Bearer jwt"},
    )
    blank_response = client.get(
        "/api/v1/web/reports/stats?is_test=",
        headers={"Authorization": "Bearer jwt"},
    )
    assert false_response.status_code == 200
    assert true_response.status_code == 200
    assert blank_response.status_code == 200
    assert [call["is_test"] for call in service.calls] == [False, True, None]


def test_stats_endpoint_reports_filter_not_ready() -> None:
    client, service = make_client()

    async def unavailable(**_kwargs: object) -> ReportingResult:
        raise ReportFilterNotReady(date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 1))

    service.get = unavailable  # type: ignore[method-assign]
    response = client.get(
        "/api/v1/web/reports/stats",
        params={"is_test": "false"},
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "REPORT_FILTER_NOT_READY",
        "message": "筛选数据尚未就绪",
        "detail": {
            "start": "2026-07-01",
            "end": "2026-07-02",
            "missing_date": "2026-07-01",
        },
    }


def test_stats_endpoint_rejects_invalid_range_and_enum() -> None:
    client, _ = make_client()
    response = client.get(
        "/api/v1/web/reports/stats?granularity=hour",
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 422


def test_stats_endpoint_requires_bearer() -> None:
    client, _ = make_client()
    response = client.get("/api/v1/web/reports/stats")
    assert response.status_code == 401


def test_stats_endpoint_validates_page_and_order_before_repository_work() -> None:
    client, service = make_client()
    for params in (
        {"page": 0},
        {"page": 1_000_001},
        {"size": 0},
        {"size": 101},
        {"sort": "total;DROP TABLE stat_daily"},
        {"order": "random"},
        {"metric": "unknown"},
    ):
        response = client.get(
            "/api/v1/web/reports/stats",
            params=params,
            headers={"Authorization": "Bearer jwt"},
        )
        assert response.status_code == 422
    assert service.calls == []
