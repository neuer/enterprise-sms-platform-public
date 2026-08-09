from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import app.core.health as health
from app.main import StartupConfigGate, create_app
from app.services.runtime_policy import DEFAULTS
from app.settings import Settings


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> list[dict[str, object]]:
        return self.rows


class FakeConnection:
    def __init__(
        self,
        *,
        now: datetime,
        partitions: set[str],
    ) -> None:
        self.scalars = iter(("0033_correlation_audit_chain", now))
        self.results = iter(
            (
                FakeResult(
                    [{"key": key, "value": value} for key, value in DEFAULTS.items()]
                ),
                FakeResult([{"relname": name} for name in sorted(partitions)]),
            )
        )

    async def scalar(self, _statement: object) -> object:
        return next(self.scalars)

    async def execute(
        self,
        _statement: object,
        _parameters: object | None = None,
    ) -> FakeResult:
        return next(self.results)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)


@pytest.mark.asyncio
async def test_database_readiness_requires_migration_config_and_future_partitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)
    partitions = set(health.required_partition_names(now, future_months=3))
    connection = FakeConnection(now=now, partitions=partitions)
    monkeypatch.setattr(
        health,
        "database_engine",
        lambda *_args, **_kwargs: FakeEngine(connection),
    )
    settings = cast(
        Any,
        SimpleNamespace(
            database_url="postgresql+asyncpg://unused",
            readiness_future_months=3,
        ),
    )

    await health.DatabaseReadinessCheck(
        settings,
        migration_head="0033_correlation_audit_chain",
    )()


@pytest.mark.asyncio
async def test_database_readiness_fails_when_future_partition_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)
    partitions = set(health.required_partition_names(now, future_months=3))
    partitions.pop()
    monkeypatch.setattr(
        health,
        "database_engine",
        lambda *_args, **_kwargs: FakeEngine(
            FakeConnection(now=now, partitions=partitions)
        ),
    )
    settings = cast(
        Any,
        SimpleNamespace(
            database_url="postgresql+asyncpg://unused",
            readiness_future_months=3,
        ),
    )

    with pytest.raises(RuntimeError, match="partitions"):
        await health.DatabaseReadinessCheck(
            settings,
            migration_head="0033_correlation_audit_chain",
        )()


@pytest.mark.asyncio
async def test_probe_timeout_and_concurrency_cap_fail_closed() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked() -> None:
        entered.set()
        await release.wait()

    probe = health.ReadinessProbe(
        (blocked,),
        timeout_seconds=0.1,
        queue_timeout_seconds=0.01,
        max_concurrency=1,
    )
    first = asyncio.create_task(probe.ready())
    await entered.wait()

    assert await probe.ready() is False
    assert await first is False


@pytest.mark.asyncio
async def test_startup_configuration_gate_recovers_without_process_restart() -> None:
    attempts = 0

    async def load() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError

    gate = StartupConfigGate(load)

    with pytest.raises(ConnectionError):
        await gate.ensure()
    assert gate.loaded is False
    await gate.ensure()
    await gate.ensure()
    assert gate.loaded is True
    assert attempts == 2


def test_readyz_response_is_minimal_for_success_and_failure() -> None:
    app = create_app()

    class Probe:
        def __init__(self, ready: bool) -> None:
            self.value = ready

        async def ready(self) -> bool:
            return self.value

    client = TestClient(app)
    app.state.readiness_probe = Probe(False)
    failed = client.get("/readyz")
    assert failed.status_code == 503
    assert failed.json() == {"status": "not_ready"}
    assert failed.headers["cache-control"] == "no-store"

    app.state.readiness_probe = Probe(True)
    succeeded = client.get("/readyz")
    assert succeeded.status_code == 200
    assert succeeded.json() == {"status": "ready"}
    assert "database" not in succeeded.text.casefold()
    assert "redis" not in succeeded.text.casefold()
    assert "secret" not in succeeded.text.casefold()


def test_runtime_secret_readiness_parses_keys_without_exposing_values(
    tmp_path: Path,
) -> None:
    encoded_key = base64.b64encode(b"k" * 32).decode()
    encoded_system_key = base64.b64encode(b"s" * 32).decode()
    values = {
        "db_accept_password": "db-password",
        "vendor_secret_name": "vendor-name",
        "vendor_secret_key": "vendor-key",
        "data_aes_key": encoded_key,
        "data_hmac_key": encoded_key,
        "audit_context_key": encoded_key,
        "audit_system_api_context_key": encoded_system_key,
        "alert_credential_public_key": encoded_key,
        "jwt_secret": "jwt-key",
        "ldap_bind_password": "ldap-password",
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        paths[name] = path
    settings = Settings(
        _env_file=None,
        environment="test",
        debug=True,
        auth_mock=True,
        vendor_mock=True,
        vendor_base_url="http://vendor-mock:9028",
        db_accept_password_file=paths["db_accept_password"],
        vendor_secret_name_file=paths["vendor_secret_name"],
        vendor_secret_key_file=paths["vendor_secret_key"],
        data_aes_key_file=paths["data_aes_key"],
        data_hmac_key_file=paths["data_hmac_key"],
        audit_context_key_file=paths["audit_context_key"],
        audit_system_api_context_key_file=paths["audit_system_api_context_key"],
        alert_credential_public_key_file=paths["alert_credential_public_key"],
        jwt_secret_file=paths["jwt_secret"],
        ldap_bind_password_file=paths["ldap_bind_password"],
    )

    assert health._validate_runtime_secrets(settings) is None
