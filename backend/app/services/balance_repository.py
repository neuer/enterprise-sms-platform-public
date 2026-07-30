"""余额阈值读取与快照 PostgreSQL 持久化。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings


class SqlBalanceRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def alert_threshold(self) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='balance_alert_threshold'")
                )
                value = result.scalar_one_or_none()
                return int(value) if value is not None else 10000
        finally:
            await engine.dispose()

    async def save_snapshot(self, balance: int) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO balance_snapshot(balance) VALUES(:balance)"),
                    {"balance": balance},
                )
        finally:
            await engine.dispose()
