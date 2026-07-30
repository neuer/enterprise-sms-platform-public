from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app.services.anomaly import AnomalyConfig, VolumeSample
from app.services.anomaly_repository import SqlAnomalyRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


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
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


class FakeRedis:
    def __init__(self, values: list[str | None]) -> None:
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.calls.append(tuple(keys))
        return self.values


def bind(repository: SqlAnomalyRepository, connection: FakeConnection) -> FakeEngine:
    engine = FakeEngine(connection)
    repository._engine = lambda: engine  # type: ignore[method-assign]
    return engine


@pytest.mark.asyncio
async def test_config_reads_enabled_multiplier_and_minimum() -> None:
    repository = SqlAnomalyRepository(redis=FakeRedis([]))
    connection = FakeConnection(
        [
            FakeResult(
                [
                    {"key": "anomaly_enabled", "value": "true"},
                    {"key": "anomaly_multiplier", "value": "4"},
                    {"key": "anomaly_min_total", "value": "800"},
                ]
            )
        ]
    )
    bind(repository, connection)

    assert await repository.config() == AnomalyConfig(True, 4, 800)


@pytest.mark.asyncio
async def test_samples_use_allowed_categories_fixed_keys_and_seven_complete_days() -> None:
    redis = FakeRedis(["600", None, "2500"])
    repository = SqlAnomalyRepository(redis=redis)
    connection = FakeConnection(
        [
            FakeResult(
                [
                    {"id": 7, "allowed_categories": "verify,notice"},
                    {"id": 9, "allowed_categories": "market"},
                ]
            ),
            FakeResult(
                [
                    {
                        "app_id": "7",
                        "category": "verify",
                        "baseline_days": 7,
                        "seven_day_total": 700,
                    },
                    {
                        "app_id": "9",
                        "category": "market",
                        "baseline_days": 6,
                        "seven_day_total": 1200,
                    },
                ]
            ),
        ]
    )
    engine = bind(repository, connection)

    assert await repository.samples(date(2026, 7, 12)) == [
        VolumeSample(7, "verify", 600, 700, 7),
        VolumeSample(7, "notice", 0, 0, 0),
        VolumeSample(9, "market", 2500, 1200, 6),
    ]
    assert redis.calls == [
        (
            "quota:volume:app:7:verify:20260712",
            "quota:volume:app:7:notice:20260712",
            "quota:volume:app:9:market:20260712",
        )
    ]
    baseline_sql, params = connection.calls[1]
    assert "total_segments" in baseline_sql
    assert params == {"start_date": date(2026, 7, 5), "end_date": date(2026, 7, 11)}
    assert engine.disposed
