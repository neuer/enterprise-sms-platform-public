"""统计日窗口与成功率唯一业务口径。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class StatsRepository(Protocol):
    """日报聚合所需的最小事实源接口。"""

    async def aggregate_day(self, stat_date: date) -> int: ...


def success_rate(delivered: int, failed: int) -> float:
    """按 delivered/(delivered+failed) 计算，unknown/other 不进入分母。"""

    if delivered < 0 or failed < 0:
        raise ValueError("message outcome counts cannot be negative")
    denominator = delivered + failed
    return delivered / denominator if denominator else 0.0


def recent_stat_dates(now: datetime) -> tuple[date, date, date]:
    """返回上海时区今天及前两个自然日，覆盖 48 小时报告回补。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("stats clock must be timezone-aware")
    today = now.astimezone(SHANGHAI).date()
    return today, today - timedelta(days=1), today - timedelta(days=2)


class StatsAggregationService:
    """串行重建最近三个统计日并汇总写入行数。"""

    def __init__(self, repository: StatsRepository) -> None:
        self.repository = repository

    async def aggregate_recent(self, now: datetime) -> int:
        total = 0
        for stat_date in recent_stat_dates(now):
            total += await self.repository.aggregate_day(stat_date)
        return total
