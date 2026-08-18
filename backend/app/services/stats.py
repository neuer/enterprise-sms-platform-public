"""统计日窗口与成功率唯一业务口径。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class StatsRepository(Protocol):
    """日报聚合所需的最小事实源接口。"""

    async def aggregate_day(self, stat_date: date) -> int: ...

    async def list_dirty_dates(self) -> tuple[date, ...]: ...

    async def clear_dirty_date(self, stat_date: date) -> None: ...


def success_rate(delivered: int, failed: int) -> float:
    """按 delivered/(delivered+failed) 计算，unknown/other 不进入分母。"""

    if delivered < 0 or failed < 0:
        raise ValueError("message outcome counts cannot be negative")
    denominator = delivered + failed
    return delivered / denominator if denominator else 0.0


def recent_stat_dates(now: datetime) -> tuple[date, ...]:
    """返回上海时区今天及前四个自然日，覆盖迟到回执回补。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("stats clock must be timezone-aware")
    today = now.astimezone(SHANGHAI).date()
    return tuple(today - timedelta(days=offset) for offset in range(5))


class StatsAggregationService:
    """串行重建最近五个统计日，并补算晚到回执标记的窗口外归属日。"""

    def __init__(self, repository: StatsRepository) -> None:
        self.repository = repository

    async def aggregate_recent(self, now: datetime) -> int:
        total = 0
        recent = recent_stat_dates(now)
        for stat_date in recent:
            total += await self.repository.aggregate_day(stat_date)
        # 晚到回执/超时过期标记的脏日：先重算成功再清除标记，聚合失败时
        # 标记保留给下一轮，统计不会永久偏离事实（#342）。
        for stat_date in await self.repository.list_dirty_dates():
            if stat_date not in recent:
                total += await self.repository.aggregate_day(stat_date)
            await self.repository.clear_dirty_date(stat_date)
        return total
