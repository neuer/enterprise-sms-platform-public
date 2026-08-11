"""上行回复掩码查询、部门隔离与不解密退订加黑。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.content_protection import decrypt_reply_content
from app.services.crypto import CryptoService
from app.settings import Settings, get_settings

PAGE_SIZE = 20


class ReplyNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ReplyItem:
    id: int
    phone_mask: str
    content: str
    batch_no: str | None
    reply_time: datetime
    blacklisted: bool


@dataclass(frozen=True, slots=True)
class ReplyPage:
    total: int
    items: tuple[ReplyItem, ...]


class ReplyQueryRepository(Protocol):
    async def list_page(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        page: int,
        dept: str | None,
    ) -> ReplyPage: ...

    async def optout(self, reply_id: int, *, dept: str | None, actor: str) -> bool: ...


class BlacklistCache(Protocol):
    async def invalidate(self) -> None: ...


class ReplyQueryService:
    def __init__(
        self,
        repository: ReplyQueryRepository,
        cache: BlacklistCache,
        crypto: CryptoService,
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.crypto = crypto

    async def list_page(
        self,
        *,
        phone: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        dept: str | None,
    ) -> ReplyPage:
        if page < 1:
            raise ValueError("page must be positive")
        for moment in (start, end):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError("query time must include timezone")
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be later than end")
        phone_hmacs = (
            tuple(self.crypto.hmac_candidates(phone).values()) if phone is not None else ()
        )
        return await self.repository.list_page(
            phone_hmacs=phone_hmacs,
            start=start,
            end=end,
            page=page,
            dept=dept,
        )

    async def optout(self, reply_id: int, *, dept: str | None, actor: str) -> None:
        if reply_id < 1:
            raise ReplyNotFound("回复不存在")
        await self.cache.invalidate()
        try:
            found = await self.repository.optout(reply_id, dept=dept, actor=actor)
        finally:
            await self.cache.invalidate()
        if not found:
            raise ReplyNotFound("回复不存在")


class SqlReplyQueryRepository:
    def __init__(
        self,
        settings: Settings | None = None,
        crypto: CryptoService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_page(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        page: int,
        dept: str | None,
    ) -> ReplyPage:
        where = """
          (CAST(:dept AS text) IS NULL OR b.dept=CAST(:dept AS text))
          AND (:has_phone=false OR r.phone_hmac = ANY(CAST(:phone_hmacs AS char(64)[])))
          AND (CAST(:start AS timestamptz) IS NULL OR r.reply_time>=CAST(:start AS timestamptz))
          AND (CAST(:end AS timestamptz) IS NULL OR r.reply_time<=CAST(:end AS timestamptz))
        """
        params = {
            "dept": dept,
            "has_phone": bool(phone_hmacs),
            "phone_hmacs": list(phone_hmacs),
            "start": start,
            "end": end,
            "limit": PAGE_SIZE,
            "offset": (page - 1) * PAGE_SIZE,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count_result = await connection.execute(
                    text(
                        "SELECT count(*) FROM sms_reply r "
                        "LEFT JOIN sms_batch b ON b.id=r.batch_id WHERE " + where
                    ),
                    params,
                )
                rows_result = await connection.execute(
                    text(
                        """
                        SELECT r.id,r.phone_mask,trim(r.event_key) event_key,
                          e.content_enc,b.batch_no,r.reply_time,
                          EXISTS(
                            SELECT 1 FROM blacklist bl WHERE bl.phone_hmac=r.phone_hmac
                          ) AS blacklisted
                        FROM sms_reply r
                        JOIN reply_event e ON e.event_key=r.event_key
                        LEFT JOIN sms_batch b ON b.id=r.batch_id
                        WHERE """
                        + where
                        + " ORDER BY r.reply_time DESC NULLS LAST,r.created_at DESC "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items: list[ReplyItem] = []
                for row in rows_result.mappings():
                    values = dict(row)
                    content = decrypt_reply_content(
                        self.crypto,
                        values.pop("content_enc"),
                        str(values.pop("event_key")),
                    )
                    items.append(ReplyItem(content=content, **values))
                return ReplyPage(int(count_result.scalar_one()), tuple(items))
        finally:
            await engine.dispose()

    async def optout(self, reply_id: int, *, dept: str | None, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO blacklist AS current(
                          phone_hmac,phone_enc,phone_mask,key_version,
                          source,remark,created_by
                        )
                        SELECT r.phone_hmac,r.phone_enc,r.phone_mask,r.key_version,
                          'reply_optout','上行回复退订',:actor
                        FROM sms_reply r LEFT JOIN sms_batch b ON b.id=r.batch_id
                        WHERE r.id=:reply_id AND r.batch_id IS NOT NULL
                          AND (:dept IS NULL OR b.dept=:dept)
                        ON CONFLICT(phone_hmac) DO UPDATE SET
                          phone_enc=excluded.phone_enc,phone_mask=excluded.phone_mask,
                          key_version=excluded.key_version,source='reply_optout',
                          remark='上行回复退订'
                        RETURNING 1
                        """
                    ),
                    {"reply_id": reply_id, "dept": dept, "actor": actor},
                )
                found = result.scalar_one_or_none() is not None
                if found:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,action,object_type,object_id,after_val
                            ) VALUES (
                              :actor,'reply_optout','blacklist',:object_id,
                              CAST(:after AS jsonb)
                            )
                            """
                        ),
                        {
                            "actor": actor,
                            "object_id": str(reply_id),
                            "after": json.dumps(
                                {"count": 1, "source": "reply_optout", "reply_id": reply_id}
                            ),
                        },
                    )
                return found
        finally:
            await engine.dispose()
