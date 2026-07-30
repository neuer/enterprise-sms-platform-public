"""应用×类别发送量异常的纯判定与告警编排。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Protocol

CATEGORIES = {"verify", "notice", "market"}
VERIFY_RECOMMENDATION = "建议：核查该应用调用来源，必要时停用 API Key 或轮换。"
UTC_PLUS_8 = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    enabled: bool
    multiplier: int
    min_total: int


@dataclass(frozen=True, slots=True)
class VolumeSample:
    app_id: int
    category: str
    current: int
    seven_day_total: int
    baseline_days: int


class AnomalyRepository(Protocol):
    async def config(self) -> AnomalyConfig: ...

    async def samples(self, scan_date: date) -> list[VolumeSample]: ...


class AlertEmitter(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
        dedup_hours: int = 4,
    ) -> None: ...


def local_date() -> date:
    """返回 +08:00 业务日期。"""

    return datetime.now(UTC).astimezone(UTC_PLUS_8).date()


def is_anomalous(sample: VolumeSample, config: AnomalyConfig) -> bool:
    """以整数交叉乘法执行双条件，7 日不足时使用五倍绝对兜底。"""

    if sample.category not in CATEGORIES:
        raise ValueError("invalid anomaly category")
    if (
        sample.app_id < 1
        or sample.current < 0
        or sample.seven_day_total < 0
        or not 0 <= sample.baseline_days <= 7
        or config.multiplier < 1
        or config.min_total < 1
    ):
        raise ValueError("invalid anomaly sample or config")
    if not config.enabled:
        return False
    if sample.baseline_days < 7:
        return sample.current >= config.min_total * 5
    return (
        sample.current >= config.min_total
        and sample.current * 7 > sample.seven_day_total * config.multiplier
    )


class AnomalyService:
    """扫描所有样本并经统一告警服务发出同日同源事件。"""

    def __init__(
        self,
        repository: AnomalyRepository,
        alerts: AlertEmitter,
        *,
        clock: Callable[[], date] = local_date,
    ) -> None:
        self.repository = repository
        self.alerts = alerts
        self.clock = clock

    async def scan(self) -> int:
        config = await self.repository.config()
        if not config.enabled:
            return 0
        scan_date = self.clock()
        alerted = 0
        for sample in await self.repository.samples(scan_date):
            if not is_anomalous(sample, config):
                continue
            detail: dict[str, Any] = {
                "app_id": sample.app_id,
                "category": sample.category,
                "current_total": sample.current,
                "baseline_days": sample.baseline_days,
                "baseline_average": (
                    round(sample.seven_day_total / 7, 2)
                    if sample.baseline_days == 7
                    else None
                ),
                "multiplier": config.multiplier,
                "min_total": config.min_total,
            }
            level = "warn"
            title = "发送量异常"
            if sample.category == "verify":
                level = "crit"
                title = "验证码发送量异常"
                detail["recommendation"] = VERIFY_RECOMMENDATION
            await self.alerts.emit(
                alert_type="anomaly",
                level=level,
                title=title,
                detail=detail,
                dedup_key=(
                    f"anomaly:{sample.app_id}:{sample.category}:{scan_date.isoformat()}"
                ),
                dedup_hours=24,
            )
            alerted += 1
        return alerted
