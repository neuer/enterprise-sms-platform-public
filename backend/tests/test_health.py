from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient


def load_main_module() -> ModuleType:
    assert importlib.util.find_spec("app.main") is not None, "app.main 尚未实现"
    return importlib.import_module("app.main")


def test_healthz_returns_minimal_non_sensitive_response() -> None:
    module = load_main_module()
    client = TestClient(module.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "server" not in {key.lower() for key in response.headers}
    live = client.get("/livez")
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert live.headers["cache-control"] == "no-store"
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    assert missing.json() == {"code": "NOT_FOUND", "message": "资源不存在", "detail": None}


def test_openapi_metadata_is_versioned() -> None:
    module = load_main_module()

    schema = module.app.openapi()

    assert schema["info"]["title"] == "企业短信管理平台 API"
    assert schema["info"]["version"] == "1.6.0"
    assert "/healthz" in schema["paths"]
    assert "/livez" in schema["paths"]
    assert "/readyz" in schema["paths"]
    assert "/metrics" in schema["paths"]


def test_production_disables_interactive_api_documentation(tmp_path: Path) -> None:
    module = load_main_module()
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")
    settings = module.Settings(
        _env_file=None,
        environment="production",
        trusted_hosts="testserver,sms.example.test",
        debug=False,
        auth_mock=False,
        vendor_mock=False,
        redis_ha_mode="managed",
        redis_broker_host="broker.redis.example",
        redis_auth_host="auth.redis.example",
        redis_control_host="control.redis.example",
        vendor_base_url="https://vendor.example.test",
        ldap_ca_certs_file=ca_file,
    )

    application = module.create_app(settings)

    assert application.docs_url is None
    assert application.redoc_url is None
    assert application.openapi_url is None


def test_create_app_stores_single_settings_source() -> None:
    module = load_main_module()
    settings = module.get_settings()

    application = module.create_app(settings)

    assert application.state.settings is settings


def test_api_lifespan_owns_job_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳巡检必须随 API 进程生命周期启动，不能依赖 beat。"""

    module = load_main_module()
    events: list[str] = []

    class FakeHeartbeat:
        def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(
        module,
        "create_default_heartbeat_service",
        lambda: FakeHeartbeat(),
    )

    async def configure_intervals() -> None:
        events.append("configure")

    monkeypatch.setattr(module, "load_and_apply_job_intervals", configure_intervals)
    with TestClient(module.create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert events == ["configure", "start"]
    assert events == ["configure", "start", "stop"]
