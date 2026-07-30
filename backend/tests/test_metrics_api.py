from __future__ import annotations

from ipaddress import ip_network
from pathlib import Path
from uuid import UUID

import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from app.api import metrics as module
from app.core.errors import ApiError, api_error_handler, internal_error_handler
from app.services.metrics import MetricsFacts, MetricsSnapshot


def without_descriptions(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: without_descriptions(item)
            for key, item in value.items()
            if key != "description"
        }
    if isinstance(value, list):
        return [without_descriptions(item) for item in value]
    return value


class FakeMetricsService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def collect(self) -> MetricsSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return MetricsSnapshot(
            queue_depths=(("realtime", 2), ("bulk", 3)),
            facts=MetricsFacts(
                send_rates=(),
                vendor_errors=(),
                uncertain=1,
                callback_failures=(),
                frequency_filtered=(),
                poll_lags=(),
            ),
        )


class FakeSettings:
    metrics_allowed_networks = (ip_network("127.0.0.1/32"),)
    token = "m" * 48

    def credential(self, name: str) -> str:
        assert name == "metrics_scrape_token"
        return self.token


def make_client(
    service: FakeMetricsService,
    *,
    client_host: str = "127.0.0.1",
) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(Exception, internal_error_handler)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[module.get_metrics_service] = lambda: service
    app.dependency_overrides[module.get_settings] = FakeSettings
    app.include_router(module.router)
    return TestClient(
        app,
        raise_server_exceptions=False,
        client=(client_host, 50000),
    )


def test_metrics_requires_independent_bearer_and_returns_prometheus_exposition() -> None:
    service = FakeMetricsService()

    response = make_client(service).get(
        "/metrics",
        headers={"Authorization": f"Bearer {'m' * 48}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "sms_queue_depth" in response.text
    assert "sms_uncertain_chunks 1.0" in response.text
    assert service.calls == 1


def test_metrics_rejects_missing_or_wrong_credentials_before_collection() -> None:
    service = FakeMetricsService()
    client = make_client(service)

    missing = client.get("/metrics")
    wrong = client.get(
        "/metrics",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert service.calls == 0


def test_metrics_rejects_source_outside_allowlist_before_collection() -> None:
    service = FakeMetricsService()

    response = make_client(service, client_host="203.0.113.8").get(
        "/metrics",
        headers={"Authorization": f"Bearer {'m' * 48}"},
    )

    assert response.status_code == 403
    assert service.calls == 0


def test_metrics_dependency_failure_is_non_2xx_and_does_not_return_stale_metrics() -> None:
    response = make_client(
        FakeMetricsService(error=RuntimeError("database unavailable"))
    ).get(
        "/metrics",
        headers={"Authorization": f"Bearer {'m' * 48}"},
    )

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message"] == "服务内部错误"
    assert UUID(body["detail"]["request_id"])
    assert "sms_queue_depth" not in response.text


def test_metrics_openapi_matches_root_level_yaml_contract() -> None:
    expected_document = yaml.safe_load(
        (Path(__file__).parents[2] / "openapi.yaml").read_text(encoding="utf-8")
    )
    route = module.router.routes[0]
    assert isinstance(route, APIRoute)
    actual_document = route.endpoint.__module__
    assert actual_document == "app.api.metrics"

    from app.main import app

    expected = expected_document["paths"]["/metrics"]["get"]
    actual_schema = app.openapi()
    actual = actual_schema["paths"]["/metrics"]["get"]
    assert actual["summary"] == expected["summary"]
    assert actual["description"] == expected["description"]
    assert actual.get("security") == expected.get("security")
    assert actual["responses"]["200"] == expected["responses"]["200"]
    assert actual["responses"]["401"]["description"] == "抓取凭据无效"
    assert actual["responses"]["403"]["description"] == "抓取源不在允许网段"
    assert actual["responses"]["500"]["description"] == "依赖不可用"
    assert without_descriptions(
        actual["responses"]["500"]["content"]["application/json"]["schema"]
    ) == without_descriptions(
        expected_document["components"]["schemas"]["Error"]
    )
