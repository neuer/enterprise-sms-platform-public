"""敏感词 PostgreSQL 仓储与无内容审计。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.sensitive import SENSITIVE_WORD_REVISION_KEY, SensitiveWord
from app.settings import Settings, get_settings


class SqlSensitiveWordRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_words(self) -> list[SensitiveWord]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT id,word FROM sensitive_word ORDER BY created_at DESC")
                )
                return [
                    SensitiveWord(int(row["id"]), str(row["word"])) for row in result.mappings()
                ]
        finally:
            await engine.dispose()

    async def all_words(self) -> list[str]:
        return [item.word for item in await self.list_words()]

    async def current_revision(self) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key=:key"),
                    {"key": SENSITIVE_WORD_REVISION_KEY},
                )
                value = result.scalar_one_or_none()
                return int(value) if value is not None else 0
        finally:
            await engine.dispose()

    async def add_many(self, words: list[str], *, actor: str) -> list[SensitiveWord]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO sensitive_word(word,created_by)
                        SELECT value,:actor FROM unnest(CAST(:words AS text[])) value
                        ON CONFLICT(word) DO NOTHING RETURNING id,word
                        """
                    ),
                    {"words": words, "actor": actor},
                )
                created = [
                    SensitiveWord(int(row["id"]), str(row["word"])) for row in result.mappings()
                ]
                if created:
                    await self._bump_revision(connection, actor)
                await self._audit(connection, actor, "sensitive_word_add", len(created))
                return created
        finally:
            await engine.dispose()

    async def delete(self, word_id: int, *, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text("DELETE FROM sensitive_word WHERE id=:id RETURNING 1"),
                    {"id": word_id},
                )
                removed = result.scalar_one_or_none() is not None
                if removed:
                    await self._bump_revision(connection, actor)
                    await self._audit(connection, actor, "sensitive_word_delete", 1)
                return removed
        finally:
            await engine.dispose()

    @staticmethod
    async def _bump_revision(connection: Any, actor: str) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO sys_config(
                  key,value,value_type,description,updated_by
                ) VALUES(
                  :key,'1','int','内部敏感词索引修订号，不可由管理接口修改',:actor
                )
                ON CONFLICT(key) DO UPDATE SET
                  value=(CAST(sys_config.value AS bigint)+1)::text,
                  updated_by=EXCLUDED.updated_by,
                  updated_at=now()
                """
            ),
            {"key": SENSITIVE_WORD_REVISION_KEY, "actor": actor},
        )

    @staticmethod
    async def _audit(connection: Any, actor: str, action: str, count: int) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                VALUES(:actor,:action,'sensitive_word','batch',CAST(:after AS jsonb))
                """
            ),
            {"actor": actor, "action": action, "after": json.dumps({"count": count})},
        )
