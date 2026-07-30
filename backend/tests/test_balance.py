from __future__ import annotations

import pytest

from app.services.balance import BalanceMonitor


class FakeVendor:
    async def get_balance(self) -> int:
        return 5000


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[int] = []

    async def alert_threshold(self) -> int:
        return 10000

    async def save_snapshot(self, balance: int) -> None:
        self.saved.append(balance)


class FakeAlertLogSink:
    def __init__(self) -> None:
        self.alert_log_rows: list[dict[str, object]] = []

    async def emit(self, **values: object) -> None:
        self.alert_log_rows.append(values)


@pytest.mark.asyncio
async def test_low_balance_persists_snapshot_and_emits_deduplicated_log_sink_event() -> None:
    repository = FakeRepository()
    sink = FakeAlertLogSink()
    assert await BalanceMonitor(repository, FakeVendor(), sink).poll() == 1
    assert repository.saved == [5000]
    assert sink.alert_log_rows == [
        {
            "alert_type": "balance_low",
            "level": "warn",
            "title": "短信厂商余额低于阈值",
            "detail": {"balance": 5000, "threshold": 10000},
            "dedup_key": "balance_low",
        }
    ]


@pytest.mark.asyncio
async def test_sufficient_balance_only_persists_snapshot() -> None:
    class HealthyVendor:
        async def get_balance(self) -> int:
            return 10000

    repository = FakeRepository()
    sink = FakeAlertLogSink()
    await BalanceMonitor(repository, HealthyVendor(), sink).poll()
    assert repository.saved == [10000]
    assert sink.alert_log_rows == []
