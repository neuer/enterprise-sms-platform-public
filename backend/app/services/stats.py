"""统计日窗口与成功率唯一业务口径。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


# 分层快照版本。1 表示同一上海自然日在一个事务内写入 all、test、normal。
STAT_TEST_SCOPE_VERSION = 1


class StatsRepository(Protocol):
    """日报聚合所需的最小事实源接口。"""

    async def aggregate_day(self, stat_date: date) -> int: ...

    async def list_dirty_dates(self) -> tuple[date, ...]: ...

    async def list_scope_backfill_dates(self) -> tuple[date, ...]: ...


def success_rate(delivered: int, failed: int) -> float:
    """按 delivered/(delivered+failed) 计算，unknown/other 不进入分母。"""

    if delivered < 0 or failed < 0:
        raise ValueError("message outcome counts cannot be negative")
    denominator = delivered + failed
    return delivered / denominator if denominator else 0.0


def success_rate_sql(delivered: str, failed: str) -> str:
    """仅为内部固定 SQL 表达式生成同一成功率口径；禁止传入用户值。"""

    return (
        f"COALESCE(CAST(({delivered}) AS double precision) / NULLIF(({delivered})+({failed}),0),0)"
    )


def recent_stat_dates(now: datetime) -> tuple[date, ...]:
    """返回上海时区今天及前四个自然日，覆盖迟到回执回补。"""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("stats clock must be timezone-aware")
    today = now.astimezone(SHANGHAI).date()
    return tuple(today - timedelta(days=offset) for offset in range(5))


class StatsAggregationService:
    """串行重建最近五个统计日，补算脏日，并按日补齐可分类的 test_scope。"""

    def __init__(self, repository: StatsRepository) -> None:
        self.repository = repository

    async def aggregate_recent(self, now: datetime) -> int:
        """滚动重建近窗与脏日，再有界补齐尚未标记的可分类日期。

        每一天单独提交。重复执行只替换该日快照，不会把三层加总。
        消息已过期、只剩 all 历史行的日期留给读取方返回未就绪，避免把长期汇总清零。
        """

        total = 0
        recent = recent_stat_dates(now)
        for stat_date in recent:
            total += await self.repository.aggregate_day(stat_date)
        # 晚到回执/超时过期标记的脏日：先重算成功再清除标记，聚合失败时
        # 标记保留给下一轮，统计不会永久偏离事实（#342）。
        for stat_date in await self.repository.list_dirty_dates():
            total += await self.repository.aggregate_day(stat_date)
        for stat_date in await self.repository.list_scope_backfill_dates():
            total += await self.repository.aggregate_day(stat_date)
        return total
