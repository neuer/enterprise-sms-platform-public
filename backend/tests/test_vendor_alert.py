from __future__ import annotations

from typing import Any

import pytest

from app.services.vendor_alert import RedisVendorAlertMonitor


class FakeRedis:
    def __init__(self, counts: list[int]) -> None:
        self.counts = iter(counts)
        self.calls: list[tuple[str, Any]] = []

    async def eval(self, script: str, numkeys: int, key: str, ttl: int) -> int:
        self.calls.append(("eval", (script, numkeys, key, ttl)))
        return next(self.counts)

    async def delete(self, key: str) -> int:
        self.calls.append(("delete", key))
        return 1


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_type"),
    [(999, "balance_blocked"), (1000, "vendor_auth_error"), (1010, "vendor_ip_error")],
)
async def test_critical_vendor_codes_emit_immediate_named_alert(
    code: int,
    expected_type: str,
) -> None:
    alerts = FakeAlerts()
    monitor = RedisVendorAlertMonitor(FakeRedis([1]), alerts)

    await monitor.record_failure(code=code, chunk_id=3, batch_id=2)

    assert alerts.events[0]["alert_type"] == expected_type
    assert alerts.events[0]["level"] == "crit"
    assert alerts.events[0]["detail"] == {
        "vendor_code": code,
        "chunk_id": 3,
        "batch_id": 2,
    }


@pytest.mark.asyncio
async def test_three_consecutive_terminal_failures_emit_deduplicated_alert() -> None:
    alerts = FakeAlerts()
    monitor = RedisVendorAlertMonitor(FakeRedis([1, 2, 3]), alerts)

    for chunk_id in (1, 2, 3):
        await monitor.record_failure(code=1002, chunk_id=chunk_id, batch_id=9)

    assert [event["alert_type"] for event in alerts.events] == [
        "vendor_consecutive_failure"
    ]
    assert alerts.events[0]["detail"]["consecutive_failures"] == 3
    assert alerts.events[0]["dedup_key"] == "vendor_consecutive_failure"


@pytest.mark.asyncio
async def test_success_deletes_consecutive_failure_counter() -> None:
    redis = FakeRedis([])
    monitor = RedisVendorAlertMonitor(redis, FakeAlerts())

    await monitor.record_success()

    assert redis.calls == [("delete", "alert:vendor:consecutive_failures")]
