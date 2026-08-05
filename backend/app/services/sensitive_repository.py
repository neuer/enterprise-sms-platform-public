"""敏感词 PostgreSQL 仓储与无内容审计。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.sensitive import (
    SENSITIVE_WORD_REVISION_KEY,
    SensitiveWord,
    SensitiveWordAddResult,
    SensitiveWordPage,
)
from app.settings import Settings, get_settings


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 ESCAPE '\\' 使用。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlSensitiveWordRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_page(
        self,
        *,
        keyword: str | None,
        page: int,
        size: int,
    ) -> SensitiveWordPage:
        where = ""
        params: dict[str, Any] = {"limit": size, "offset": (page - 1) * size}
        if keyword is not None:
            where = " WHERE word ILIKE :keyword"
            params["keyword"] = f"%{_escape_like(keyword)}%"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                total = await connection.scalar(
                    text(f"SELECT count(*) FROM sensitive_word{where}"),  # noqa: S608
                    params,
                )
                result = await connection.execute(
                    text(
                        f"""
                        SELECT id,word,created_at FROM sensitive_word{where}
                        ORDER BY created_at DESC LIMIT :limit OFFSET :offset
                        """  # noqa: S608
                    ),
                    params,
                )
                return SensitiveWordPage(
                    total=int(total or 0),
                    items=[
                        SensitiveWord(int(row["id"]), str(row["word"]), row["created_at"])
                        for row in result.mappings()
                    ],
                )
        finally:
            await engine.dispose()

    async def all_words(self) -> list[str]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT word FROM sensitive_word"))
                return [str(value) for value in result.scalars()]
        finally:
            await engine.dispose()

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

    async def add_many(self, words: list[str], *, actor: str) -> SensitiveWordAddResult:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO sensitive_word(word,created_by)
                        SELECT value,:actor FROM unnest(CAST(:words AS text[])) value
                        ON CONFLICT(word) DO NOTHING RETURNING id,word,created_at
                        """
                    ),
                    {"words": words, "actor": actor},
                )
                created = [
                    SensitiveWord(int(row["id"]), str(row["word"]), row["created_at"])
                    for row in result.mappings()
                ]
                if created:
                    await self._bump_revision(connection, actor)
                await self._audit(connection, actor, "sensitive_word_add", len(created))
                return SensitiveWordAddResult(created, skipped=len(words) - len(created))
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
