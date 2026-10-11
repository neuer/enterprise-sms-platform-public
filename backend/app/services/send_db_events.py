"""发送事务固定失败原因的跨 worker 计数及有限时间告警。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.alert_repository import SqlAlertService
from app.services.send_db_errors import SEND_DB_REASONS
from app.settings import Settings

LOGGER = logging.getLogger(__name__)
SEND_DB_PHASES = frozenset({"claim", "finalize"})


async def observe_send_db_failure(
    settings: Settings,
    *,
    phase: str,
    reason: str,
    chunk_id: int,
    attempt_id: int,
    generation: int,
    alert: bool,
) -> None:
    """业务事务已经退出；日志先行，数据库不可用时不阻断事实回读或制造无限重试。"""
    if phase not in SEND_DB_PHASES or reason not in SEND_DB_REASONS:
        raise ValueError("invalid send database event")
    context = {
        "phase": phase,
        "reason": reason,
        "chunk_id": chunk_id,
        "attempt_id": attempt_id,
        "generation": generation,
    }
    LOGGER.warning("send_db_failure", extra=context)
    try:
        async with asyncio.timeout(2):
            engine = database_engine(settings.database_url)
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                        INSERT INTO send_db_failure_event(id,phase,reason)
                        VALUES(CAST(:id AS uuid),:phase,:reason)
                    """),
                    {"id": str(uuid4()), "phase": phase, "reason": reason},
                )
            if alert:
                await SqlAlertService(settings).emit(
                    alert_type="send_db_failure",
                    level="crit",
                    title="发送结果事务失败，请核验数据库事实，禁止直接重发",
                    detail=context,
                    dedup_key=f"send_db_failure:{phase}:{reason}",
                )
    except Exception:
        # 故障数据库可能同时无法保存计数/告警；明确记录观测缺口，不声称计数完整。
        LOGGER.error("send_db_observation_unavailable", extra={"phase": phase, "reason": reason})
