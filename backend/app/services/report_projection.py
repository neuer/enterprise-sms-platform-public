"""发送确认只能推进无报告消息；已认证的报告投影可幂等恢复。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# 共享写入条件：字段不完整也不把来源不明的报告当作“尚无报告”。
NO_REPORT_EVIDENCE = (
    "report_event_key IS NULL AND report_status IS NULL "
    "AND report_time IS NULL AND report_desc IS NULL"
)

TRUSTED_REPORT_EVIDENCE = """
EXISTS (
  SELECT 1 FROM report_event e
  JOIN report_event_projection p ON p.event_key=e.event_key
  WHERE m.report_event_key=e.event_key AND p.projection_changed
    AND p.message_id=m.id AND p.message_created_at=m.created_at AND p.batch_id=m.batch_id
    AND e.message_status=CASE WHEN e.report_status=1 THEN 'delivered'
      WHEN e.report_status IN (2,99) THEN 'failed'
      WHEN e.report_status=0 THEN 'unknown' ELSE 'other' END
)
"""


async def repair_message_projection_from_report(
    connection: AsyncConnection,
    *,
    batch_id: int,
    identities: Sequence[tuple[int, datetime]],
) -> list[dict[str, Any]]:
    """在批次及消息锁内恢复已选中的可信事实，不重排回执、不创造新事件。"""

    if not identities:
        return []
    result = await connection.execute(
        text(
            f"""
            UPDATE sms_message m SET status=e.message_status,
              report_status=e.report_status, report_desc=e.report_desc,
              report_time=e.report_time
            FROM report_event e,
              unnest(CAST(:ids AS bigint[]), CAST(:created_ats AS timestamptz[]))
                AS chosen(id, created_at)
            WHERE m.id=chosen.id AND m.created_at=chosen.created_at
              AND m.batch_id=:batch_id
              AND m.report_event_key=e.event_key AND {TRUSTED_REPORT_EVIDENCE}
              AND (m.status,m.report_status,m.report_desc,m.report_time)
                IS DISTINCT FROM
                  (e.message_status,e.report_status,e.report_desc,e.report_time)
            RETURNING m.id,m.created_at,m.report_event_key,m.status,
                      m.report_desc,m.report_time
            """
        ),
        {
            "batch_id": batch_id,
            "ids": [identity[0] for identity in identities],
            "created_ats": [identity[1] for identity in identities],
        },
    )
    rows = [dict(row) for row in result.mappings()]
    if rows:
        await connection.execute(
            text(
                """
                INSERT INTO stat_dirty_date(stat_date)
                SELECT DISTINCT CAST(at AT TIME ZONE 'Asia/Shanghai' AS date)
                FROM unnest(CAST(:ats AS timestamptz[])) AS at
                ON CONFLICT(stat_date) DO NOTHING
                """
            ),
            {"ats": [row["created_at"] for row in rows]},
        )
    return rows
