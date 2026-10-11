"""跨 worker 的轮询续租故障计数；不保存 Redis key、token 或响应。"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from app.core.redis_lock import LEASE_FAILURE_REASONS
from app.core.runtime_resources import database_engine
from app.settings import Settings


class SqlPollLeaseEvents:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def record(self, source: str, reason: str) -> None:
        """每次持有的首次故障记录一次；UUID 是观测引用，绝不是锁 token。"""
        if source not in {"report", "reply"} or reason not in LEASE_FAILURE_REASONS:
            raise ValueError("invalid poll lease event")
        engine = database_engine(self.settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO worker_lease_event(task_kind,task_id,event_type,lease_id)
                    VALUES(:kind,1,:reason,CAST(:observation AS uuid))
                """),
                {"kind": f"poll_{source}", "reason": reason, "observation": str(uuid4())},
            )
