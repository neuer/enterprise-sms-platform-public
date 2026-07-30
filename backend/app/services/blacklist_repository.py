"""黑名单 PostgreSQL 事实源；审计只记录数量，不记录号码或号码派生列表。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.blacklist import BlacklistEntry
from app.settings import Settings, get_settings


class SqlBlacklistRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_entries(self) -> list[BlacklistEntry]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT phone_hmac,phone_enc,phone_mask,key_version,source,remark,created_at
                        FROM blacklist ORDER BY created_at DESC
                        """
                    )
                )
                return [BlacklistEntry(**dict(row)) for row in result.mappings()]
        finally:
            await engine.dispose()

    async def all_hmacs(self) -> set[str]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT phone_hmac FROM blacklist"))
                return {str(value).strip() for value in result.scalars()}
        finally:
            await engine.dispose()

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        actor: str,
        source: str,
    ) -> int:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO blacklist(
                          phone_hmac,phone_enc,phone_mask,key_version,source,remark,created_by
                        ) VALUES (
                          :phone_hmac,:phone_enc,:phone_mask,:key_version,:source,:remark,:actor
                        ) ON CONFLICT(phone_hmac) DO UPDATE SET
                          phone_enc=excluded.phone_enc,phone_mask=excluded.phone_mask,
                          key_version=excluded.key_version,source=excluded.source,
                          remark=excluded.remark
                        """
                    ),
                    [
                        {
                            "phone_hmac": item.phone_hmac,
                            "phone_enc": item.phone_enc,
                            "phone_mask": item.phone_mask,
                            "key_version": item.key_version,
                            "source": source,
                            "remark": item.remark,
                            "actor": actor,
                        }
                        for item in entries
                    ],
                )
                await self._audit(connection, actor, "blacklist_add", len(entries), source)
                return len(entries)
        finally:
            await engine.dispose()

    async def delete(self, phone_hmac: str, *, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text("DELETE FROM blacklist WHERE phone_hmac=:phone_hmac RETURNING 1"),
                    {"phone_hmac": phone_hmac},
                )
                removed = result.scalar_one_or_none() is not None
                if removed:
                    await self._audit(connection, actor, "blacklist_delete", 1, None)
                return removed
        finally:
            await engine.dispose()

    @staticmethod
    async def _audit(
        connection: Any,
        actor: str,
        action: str,
        count: int,
        source: str | None,
    ) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                VALUES(:actor,:action,'blacklist','batch',CAST(:after AS jsonb))
                """
            ),
            {
                "actor": actor,
                "action": action,
                "after": json.dumps({"count": count, "source": source}),
            },
        )
