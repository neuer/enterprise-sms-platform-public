from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.anomaly import (
    AnomalyConfig,
    AnomalyService,
    VolumeSample,
    is_anomalous,
)


class FakeRepository:
    def __init__(self, config: AnomalyConfig, samples: list[VolumeSample]) -> None:
        self.value = config
        self.values = samples
        self.sample_calls: list[date] = []

    async def config(self) -> AnomalyConfig:
        return self.value

    async def samples(self, scan_date: date) -> list[VolumeSample]:
        self.sample_calls.append(scan_date)
        return self.values


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def sample(
    *,
    category: str = "notice",
    current: int = 500,
    seven_day_total: int = 700,
    baseline_days: int = 7,
) -> VolumeSample:
    return VolumeSample(7, category, current, seven_day_total, baseline_days)


def test_baseline_path_requires_multiplier_and_absolute_minimum_together() -> None:
    config = AnomalyConfig(True, multiplier=3, min_total=500)

    assert not is_anomalous(sample(current=499, seven_day_total=700), config)
    assert not is_anomalous(sample(current=300, seven_day_total=700), config)
    assert not is_anomalous(
        sample(current=300, seven_day_total=1050), AnomalyConfig(True, 2, 300)
    )
    assert is_anomalous(
        sample(current=301, seven_day_total=1050), AnomalyConfig(True, 2, 300)
    )


def test_incomplete_baseline_uses_five_times_absolute_fallback() -> None:
    config = AnomalyConfig(True, multiplier=3, min_total=500)

    assert not is_anomalous(sample(current=2499, baseline_days=6), config)
    assert is_anomalous(sample(current=2500, baseline_days=0, seven_day_total=0), config)


@pytest.mark.asyncio
async def test_verify_anomaly_is_crit_with_required_response_recommendation() -> None:
    scan_date = date(2026, 7, 12)
    repository = FakeRepository(
        AnomalyConfig(True, 3, 500),
        [sample(category="verify", current=600, seven_day_total=700)],
    )
    alerts = FakeAlerts()

    assert await AnomalyService(repository, alerts, clock=lambda: scan_date).scan() == 1

    event = alerts.events[0]
    assert event["alert_type"] == "anomaly"
    assert event["level"] == "crit"
    assert "核查该应用调用来源" in event["detail"]["recommendation"]
    assert "停用 API Key 或轮换" in event["detail"]["recommendation"]
    assert event["dedup_key"] == "anomaly:7:verify:2026-07-12"
    assert event["dedup_hours"] == 24


@pytest.mark.asyncio
async def test_non_verify_anomaly_is_warn_and_same_source_has_stable_daily_key() -> None:
    alerts = FakeAlerts()
    repository = FakeRepository(
        AnomalyConfig(True, 3, 500),
        [sample(category="market", current=600, seven_day_total=700)],
    )

    assert await AnomalyService(
        repository,
        alerts,
        clock=lambda: date(2026, 7, 12),
    ).scan() == 1
    assert alerts.events[0]["level"] == "warn"
    assert alerts.events[0]["dedup_key"] == "anomaly:7:market:2026-07-12"


@pytest.mark.asyncio
async def test_disabled_detection_does_not_read_volume_samples() -> None:
    repository = FakeRepository(AnomalyConfig(False, 3, 500), [sample(current=10_000)])

    assert await AnomalyService(
        repository,
        FakeAlerts(),
        clock=lambda: date(2026, 7, 12),
    ).scan() == 0
    assert repository.sample_calls == []
