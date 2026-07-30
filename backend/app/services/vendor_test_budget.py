"""真实厂商受控联调的每日计费条预留与结算原语。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

LIVE_TEST_DAILY_SEGMENT_LIMIT = 100
SHANGHAI = ZoneInfo("Asia/Shanghai")
LiveTestSettlement = Literal["confirmed", "uncertain", "released"]


class SubmissionClaimStatus(StrEnum):
    """发送前原子认领结果。"""

    CLAIMED = "claimed"
    STALE = "stale"
    DAILY_LIMIT = "daily_limit"


@dataclass(frozen=True)
class SubmissionClaim:
    """认领结果；达到日上限时携带下个上海自然日边界。"""

    status: SubmissionClaimStatus
    reset_at: datetime | None = None


def current_live_test_time() -> datetime:
    """返回可被测试替换的当前绝对时间。"""

    return datetime.now(UTC)


def live_test_usage_window(now: datetime) -> tuple[date, datetime]:
    """按上海时区计算账本日期和下一自然日零点。"""

    if now.tzinfo is None:
        raise ValueError("live-test clock must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    usage_date = local_now.date()
    reset_at = datetime.combine(usage_date + timedelta(days=1), time.min, SHANGHAI)
    return usage_date, reset_at


async def settle_live_test_attempt(
    connection: AsyncConnection,
    chunk_id: int,
    status: LiveTestSettlement,
) -> int:
    """原子结算当前 reserved attempt；非真实联调分片自然 no-op。"""

    if status not in {"confirmed", "uncertain", "released"}:
        raise ValueError("unsupported live-test settlement")
    confirmed_expression = (
        "u.confirmed_segments+settled.segments"
        if status == "confirmed"
        else "u.confirmed_segments"
    )
    uncertain_expression = (
        "u.uncertain_segments+settled.segments"
        if status == "uncertain"
        else "u.uncertain_segments"
    )
    result = await connection.execute(
        text(
            f"""
            WITH settled AS (
              UPDATE vendor_test_send_attempt
              SET status=:status,settled_at=now()
              WHERE chunk_id=:chunk_id AND status='reserved'
              RETURNING usage_date,segments
            )
            UPDATE vendor_test_daily_usage u SET
              in_flight_segments=u.in_flight_segments-settled.segments,
              confirmed_segments={confirmed_expression},
              uncertain_segments={uncertain_expression},
              updated_at=now()
            FROM settled
            WHERE u.usage_date=settled.usage_date
            """
        ),
        {"chunk_id": chunk_id, "status": status},
    )
    return int(result.rowcount)
