"""异常检测的 sys_config、应用类别、Redis 当日量与七日基线仓储。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.anomaly import CATEGORIES, AnomalyConfig, VolumeSample
from app.settings import Settings, get_settings


class SqlAnomalyRepository:
    """PostgreSQL 提供配置/基线，Redis 仅提供当日类别配额计数。"""

    def __init__(self, settings: Settings | None = None, redis: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis: Any = redis or Redis.from_url(
            self.settings.redis_control_url,
            decode_responses=True,
        )

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def config(self) -> AnomalyConfig:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key,value FROM sys_config WHERE key IN (
                          'anomaly_enabled','anomaly_multiplier','anomaly_min_total'
                        )
                        """
                    )
                )
                values = {str(row["key"]): str(row["value"]) for row in result.mappings()}
        finally:
            await engine.dispose()
        return AnomalyConfig(
            enabled=values.get("anomaly_enabled", "true").casefold() == "true",
            multiplier=int(values.get("anomaly_multiplier", "3")),
            min_total=int(values.get("anomaly_min_total", "500")),
        )

    async def samples(self, scan_date: date) -> list[VolumeSample]:
        start_date = scan_date - timedelta(days=7)
        end_date = scan_date - timedelta(days=1)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                apps_result = await connection.execute(
                    text("SELECT id,allowed_categories FROM app WHERE status=1 ORDER BY id")
                )
                dimensions: list[tuple[int, str]] = []
                for row in apps_result.mappings():
                    app_id = int(row["id"])
                    categories = str(row["allowed_categories"] or "").split(",")
                    dimensions.extend(
                        (app_id, category.strip())
                        for category in categories
                        if category.strip() in CATEGORIES
                    )
                if not dimensions:
                    return []
                baseline_result = await connection.execute(
                    text(
                        """
                        SELECT dim_value app_id,category,
                          count(DISTINCT stat_date) baseline_days,
                          COALESCE(sum(total_segments),0) seven_day_total
                        FROM stat_daily
                        WHERE dim_type='app'
                          AND category IN ('verify','notice','market')
                          AND stat_date BETWEEN :start_date AND :end_date
                        GROUP BY dim_value,category
                        """
                    ),
                    {"start_date": start_date, "end_date": end_date},
                )
                baselines = {
                    (int(row["app_id"]), str(row["category"])): (
                        int(row["seven_day_total"]),
                        int(row["baseline_days"]),
                    )
                    for row in baseline_result.mappings()
                }
        finally:
            await engine.dispose()
        date_key = scan_date.strftime("%Y%m%d")
        keys = [
            f"quota:volume:app:{app_id}:{category}:{date_key}" for app_id, category in dimensions
        ]
        raw_current = await self.redis.mget(keys)
        samples: list[VolumeSample] = []
        for (app_id, category), value in zip(dimensions, raw_current, strict=True):
            seven_day_total, baseline_days = baselines.get((app_id, category), (0, 0))
            samples.append(
                VolumeSample(
                    app_id,
                    category,
                    int(value or 0),
                    seven_day_total,
                    baseline_days,
                )
            )
        return samples
