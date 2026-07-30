from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

import app.core.runtime_resources as resources


class FakePool:
    def __init__(
        self,
        *,
        checked_in: int = 0,
        checked_out: int = 0,
    ) -> None:
        self.checked_in = checked_in
        self.checked_out = checked_out

    def checkedin(self) -> int:
        return self.checked_in

    def checkedout(self) -> int:
        return self.checked_out


class FakeSyncEngine:
    def __init__(self) -> None:
        self.discarded = False

    def dispose(self, *, close: bool) -> None:
        assert close is False
        self.discarded = True


class FakeConnectionContext:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def __aenter__(self) -> object:
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(
        self,
        *,
        checked_in: int = 0,
        checked_out: int = 0,
        connect_error: Exception | None = None,
    ) -> None:
        self.pool = FakePool(checked_in=checked_in, checked_out=checked_out)
        self.sync_engine = FakeSyncEngine()
        self.connect_error = connect_error
        self.closed = False

    def connect(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.connect_error)

    def begin(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.connect_error)

    async def dispose(self) -> None:
        self.closed = True


class FakeRedisClient:
    def __init__(self) -> None:
        self.closed = False
        self.connection_pool = type("RedisPool", (), {})()
        vars(self.connection_pool)["_in_use_connections"] = set()
        vars(self.connection_pool)["_available_connections"] = []

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_runtime_resources() -> None:
    resources._DATABASE_ENGINES.clear()
    resources._DATABASE_METRICS.clear()
    resources._REDIS_CLIENTS.clear()
    resources._BUDGETS.clear()
    resources._BUDGETS.update(resources.DEFAULT_BUDGETS)
    resources._RUNTIME_COMPONENT = "api"
    yield
    resources._DATABASE_ENGINES.clear()
    resources._DATABASE_METRICS.clear()
    resources._REDIS_CLIENTS.clear()
    resources._BUDGETS.clear()
    resources._BUDGETS.update(resources.DEFAULT_BUDGETS)
    resources._RUNTIME_COMPONENT = "api"


def test_database_engine_reuses_bounded_pool_per_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[FakeEngine] = []
    options: list[dict[str, Any]] = []

    def engine_factory(*_args: object, **kwargs: Any) -> FakeEngine:
        options.append(kwargs)
        engine = FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(resources, "create_async_engine", engine_factory)

    first = resources.database_engine("postgresql+asyncpg://db")
    assert first is resources.database_engine("postgresql+asyncpg://db")
    metrics = resources.database_engine(
        "postgresql+asyncpg://db",
        component="metrics",
    )

    assert first is not metrics
    assert len(engines) == 2
    assert options[0] == {
        "pool_size": 8,
        "max_overflow": 2,
        "pool_timeout": 3.0,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "connect_args": {
            "timeout": 3.0,
            "server_settings": {
                "application_name": "sms-api",
                "statement_timeout": "15000",
                "search_path": "pg_catalog,public",
            },
        },
    }
    assert options[1]["pool_size"] == 2
    assert options[1]["max_overflow"] == 0
    assert options[1]["connect_args"]["server_settings"] == {
        "application_name": "sms-metrics",
        "statement_timeout": "2000",
        "search_path": "pg_catalog,public",
    }


def test_redis_client_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[FakeRedisClient] = []

    def redis_factory(*_args: object, **kwargs: object) -> FakeRedisClient:
        assert kwargs == {"decode_responses": True}
        client = FakeRedisClient()
        clients.append(client)
        return client

    class FakeRedis:
        from_url = staticmethod(redis_factory)

    monkeypatch.setattr(resources, "Redis", FakeRedis)

    assert resources.redis_client("redis://cache") is resources.redis_client(
        "redis://cache"
    )
    assert len(clients) == 1


def test_concurrent_callers_create_only_one_bounded_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeEngine] = []

    def factory(*_args: object, **_kwargs: object) -> FakeEngine:
        engine = FakeEngine()
        created.append(engine)
        return engine

    monkeypatch.setattr(resources, "create_async_engine", factory)
    with ThreadPoolExecutor(max_workers=16) as executor:
        engines = list(
            executor.map(
                resources.database_engine,
                ["postgresql+asyncpg://db"] * 100,
            )
        )

    assert len({id(engine) for engine in engines}) == 1
    assert len(created) == 1


@pytest.mark.asyncio
async def test_acquisition_wait_and_timeout_are_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = [
        FakeEngine(),
        FakeEngine(connect_error=SqlAlchemyTimeoutError()),
    ]
    monkeypatch.setattr(
        resources,
        "create_async_engine",
        lambda *_args, **_kwargs: created.pop(0),
    )

    engine = resources.database_engine("postgresql+asyncpg://healthy")
    async with engine.connect():
        pass
    failing = resources.database_engine("postgresql+asyncpg://unavailable")
    with pytest.raises(SqlAlchemyTimeoutError):
        async with failing.connect():
            pass

    snapshot = resources.resource_snapshot().database_components[0]
    assert snapshot.component == "api"
    assert snapshot.acquisitions == 2
    assert snapshot.wait_seconds >= 0
    assert snapshot.timeouts == 1


@pytest.mark.asyncio
async def test_pool_recovers_after_temporary_connection_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveringEngine(FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def connect(self) -> FakeConnectionContext:
            self.attempts += 1
            if self.attempts == 1:
                return FakeConnectionContext(SqlAlchemyTimeoutError())
            return FakeConnectionContext()

    underlying = RecoveringEngine()
    monkeypatch.setattr(
        resources,
        "create_async_engine",
        lambda *_args, **_kwargs: underlying,
    )
    engine = resources.database_engine("postgresql+asyncpg://recovering")

    with pytest.raises(SqlAlchemyTimeoutError):
        async with engine.connect():
            pass
    async with engine.connect():
        pass

    assert underlying.attempts == 2
    snapshot = resources.resource_snapshot().database_components[0]
    assert snapshot.acquisitions == 2
    assert snapshot.timeouts == 1


@pytest.mark.asyncio
async def test_close_records_checked_out_leak_and_closes_all_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(checked_in=2, checked_out=1)
    client = FakeRedisClient()
    monkeypatch.setattr(
        resources,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    resources.database_engine("postgresql+asyncpg://db")
    resources._REDIS_CLIENTS["redis"] = client

    await resources.close_runtime_resources()

    assert engine.closed and client.closed
    assert resources._DATABASE_ENGINES == {}
    assert resources._REDIS_CLIENTS == {}
    snapshot = resources.resource_snapshot().database_components[0]
    assert snapshot.leaked_on_shutdown == 1


@pytest.mark.asyncio
async def test_close_attempts_every_resource_when_one_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()

    class FailingRedis(FakeRedisClient):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("redis close failed")

    client = FailingRedis()
    monkeypatch.setattr(
        resources,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    resources.database_engine("postgresql+asyncpg://db")
    resources._REDIS_CLIENTS["redis"] = client

    with pytest.raises(RuntimeError, match="resources failed to close"):
        await resources.close_runtime_resources()

    assert client.closed is True
    assert engine.closed is True


def test_prefork_discards_inherited_pool_without_closing_parent_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(
        resources,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    resources.database_engine("postgresql+asyncpg://db")
    resources._record_acquisition("api", 0.1, timed_out=False)

    resources.discard_inherited_runtime_resources()

    assert engine.sync_engine.discarded is True
    assert engine.closed is False
    assert resources._DATABASE_ENGINES == {}
    assert resources._DATABASE_METRICS == {}


def test_request_worker_and_metric_paths_cannot_create_private_engines() -> None:
    app_root = Path(__file__).parents[1] / "app"
    exemptions = {
        app_root / "cli.py",
        app_root / "core" / "runtime_resources.py",
    }
    offenders = []
    for path in app_root.rglob("*.py"):
        if path in exemptions:
            continue
        source = path.read_text(encoding="utf-8")
        if "create_async_engine(" in source or "NullPool" in source:
            offenders.append(path.relative_to(app_root).as_posix())

    assert offenders == []
