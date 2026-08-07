"""批次运营查询、号码 HMAC 检索与授权解密的领域边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.services.batch_query import BatchAccessScope
from app.services.crypto import CryptoService
from app.settings import Settings, get_settings


class QueryNotFound(LookupError):
    """查询对象不存在或不在调用方数据权限内。"""


TIMELINE_EVENT_LIMIT = 500
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class MessageQueryItem:
    id: int
    phone_mask: str
    status: str
    report_desc: str | None
    report_time: datetime | None
    created_at: datetime
    batch_no: str
    category: str
    content: str
    sender: str | None


@dataclass(frozen=True, slots=True)
class MessageQueryPage:
    total: int
    items: tuple[MessageQueryItem, ...]


@dataclass(frozen=True, slots=True)
class PhoneBadge:
    blacklisted: bool
    blacklist_source: str | None
    recv_30d: int


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    ts: datetime
    direction: str
    category: str | None
    batch_no: str | None
    content: str
    status: str | None
    sender: str | None


@dataclass(frozen=True, slots=True)
class Timeline:
    badge: PhoneBadge
    events: tuple[TimelineEvent, ...]
    truncated: bool = False


class OperationsQueryRepository(Protocol):
    async def search_messages(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        category: str | None,
        status: str | None,
        page: int,
        size: int,
        scope: BatchAccessScope,
    ) -> MessageQueryPage: ...

    async def timeline(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        scope: BatchAccessScope,
    ) -> Timeline: ...

    async def authorized_phone(
        self,
        message_id: int,
        *,
        scope: BatchAccessScope,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[bytes, int, str] | None: ...


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    for moment in (start, end):
        if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
            raise ValueError("query time must include timezone")
    if start is not None and end is not None and start > end:
        raise ValueError("start must not be later than end")


class OperationsQueryService:
    """把手机号立即转换为多版本 HMAC；明文不进入仓储或日志。"""

    def __init__(
        self,
        repository: OperationsQueryRepository,
        crypto: CryptoService,
    ) -> None:
        self.repository = repository
        self.crypto = crypto

    def _hmacs(self, phone: str) -> tuple[str, ...]:
        return tuple(self.crypto.hmac_candidates(phone).values())

    async def search_messages(
        self,
        *,
        phone: str,
        start: datetime | None,
        end: datetime | None,
        category: str | None,
        status: str | None,
        page: int,
        size: int,
        scope: BatchAccessScope,
    ) -> MessageQueryPage:
        if page < 1:
            raise ValueError("page must be positive")
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError("page size out of range")
        _validate_range(start, end)
        return await self.repository.search_messages(
            phone_hmacs=self._hmacs(phone),
            start=start,
            end=end,
            category=category,
            status=status,
            page=page,
            size=size,
            scope=scope,
        )

    async def timeline(
        self,
        *,
        phone: str,
        start: datetime | None,
        end: datetime | None,
        scope: BatchAccessScope,
    ) -> Timeline:
        _validate_range(start, end)
        return await self.repository.timeline(
            phone_hmacs=self._hmacs(phone),
            start=start,
            end=end,
            scope=scope,
        )

    async def decrypt_phone(
        self,
        message_id: int,
        *,
        scope: BatchAccessScope,
        principal: SecurityPrincipal,
        ip: str,
    ) -> str:
        if message_id < 1:
            raise QueryNotFound("消息不存在")
        protected = await self.repository.authorized_phone(
            message_id,
            scope=scope,
            principal=principal,
            ip=ip,
        )
        if protected is None:
            raise QueryNotFound("消息不存在")
        return self.crypto.decrypt_phone(protected[0], protected[1], protected[2])


class SqlOperationsQueryRepository:
    """PostgreSQL 查询事实源；普通查询永不投影手机号密文或 HMAC。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def search_messages(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        category: str | None,
        status: str | None,
        page: int,
        size: int,
        scope: BatchAccessScope,
    ) -> MessageQueryPage:
        predicate, scope_params = scope.sql()
        where = f"""
          m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
          AND (CAST(:start AS timestamptz) IS NULL OR m.created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR m.created_at<=:end)
          AND (CAST(:category AS varchar(10)) IS NULL OR b.category=:category)
          AND (CAST(:status AS varchar(10)) IS NULL OR m.status=:status)
          AND {predicate}
        """
        params = {
            "phone_hmacs": list(phone_hmacs),
            "start": start,
            "end": end,
            "category": category,
            "status": status,
            "limit": size,
            "offset": (page - 1) * size,
            **scope_params,
        }
        source = " FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id "
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count_result = await connection.execute(
                    text("SELECT count(*)" + source + "WHERE " + where),
                    params,
                )
                rows_result = await connection.execute(
                    text(
                        """
                        SELECT m.id,m.phone_mask,m.status,m.report_desc,m.report_time,
                          m.created_at,trim(b.batch_no) batch_no,b.category,b.content,
                          CASE WHEN b.channel='web' THEN b.creator ELSE a.name END sender
                        """
                        + source
                        + " LEFT JOIN app a ON a.id=b.app_id WHERE "
                        + where
                        + " ORDER BY m.created_at DESC,m.id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                return MessageQueryPage(
                    int(count_result.scalar_one()),
                    tuple(MessageQueryItem(**dict(row)) for row in rows_result.mappings()),
                )
        finally:
            await engine.dispose()

    async def timeline(
        self,
        *,
        phone_hmacs: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
        scope: BatchAccessScope,
    ) -> Timeline:
        predicate, scope_params = scope.sql()
        params = {
            "phone_hmacs": list(phone_hmacs),
            "start": start,
            "end": end,
            "event_limit": TIMELINE_EVENT_LIMIT + 1,
            **scope_params,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                blacklist_result = await connection.execute(
                    text(
                        f"""
                        SELECT bl.source FROM blacklist bl
                        WHERE bl.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                          AND EXISTS (
                            SELECT 1 FROM sms_message m
                            JOIN sms_batch b ON b.id=m.batch_id
                            WHERE m.phone_hmac=bl.phone_hmac AND {predicate}
                            UNION ALL
                            SELECT 1 FROM sms_reply r
                            LEFT JOIN sms_batch b ON b.id=r.batch_id
                            WHERE r.phone_hmac=bl.phone_hmac AND {predicate}
                          )
                        ORDER BY bl.created_at DESC LIMIT 1
                        """
                    ),
                    params,
                )
                blacklist_row = blacklist_result.mappings().first()
                received_result = await connection.execute(
                    text(
                        f"""
                        SELECT count(*) FROM sms_message m
                        JOIN sms_batch b ON b.id=m.batch_id
                        WHERE m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                          AND m.created_at>=now()-interval '30 days' AND {predicate}
                        """
                    ),
                    params,
                )
                events_result = await connection.execute(
                    text(
                        f"""
                        SELECT * FROM (
                          SELECT m.created_at ts,'out' direction,b.category,
                            trim(b.batch_no) batch_no,b.content,m.status,
                            CASE WHEN b.channel='web' THEN b.creator ELSE a.name END sender
                          FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                          LEFT JOIN app a ON a.id=b.app_id
                          WHERE m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                            AND (CAST(:start AS timestamptz) IS NULL OR m.created_at>=:start)
                            AND (CAST(:end AS timestamptz) IS NULL OR m.created_at<=:end)
                            AND {predicate}
                          UNION ALL
                          SELECT COALESCE(r.reply_time,r.created_at) ts,'in' direction,
                            b.category,trim(b.batch_no) batch_no,r.content,NULL status,
                            '用户' sender
                          FROM sms_reply r LEFT JOIN sms_batch b ON b.id=r.batch_id
                          WHERE r.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))
                            AND (CAST(:start AS timestamptz) IS NULL
                              OR COALESCE(r.reply_time,r.created_at)>=:start)
                            AND (CAST(:end AS timestamptz) IS NULL
                              OR COALESCE(r.reply_time,r.created_at)<=:end)
                            AND {predicate}
                        ) events ORDER BY ts DESC,direction LIMIT :event_limit
                        """
                    ),
                    params,
                )
                rows = [dict(row) for row in events_result.mappings()]
                truncated = len(rows) > TIMELINE_EVENT_LIMIT
                return Timeline(
                    PhoneBadge(
                        blacklist_row is not None,
                        str(blacklist_row["source"]) if blacklist_row is not None else None,
                        int(received_result.scalar_one()),
                    ),
                    tuple(
                        TimelineEvent(**row) for row in rows[:TIMELINE_EVENT_LIMIT]
                    ),
                    truncated,
                )
        finally:
            await engine.dispose()

    async def authorized_phone(
        self,
        message_id: int,
        *,
        scope: BatchAccessScope,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[bytes, int, str] | None:
        predicate, scope_params = scope.sql()
        params = {"message_id": message_id, **scope_params}
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT m.phone_enc,m.key_version,trim(m.phone_hmac) phone_hmac,
                          trim(b.batch_no) batch_no
                        FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                        WHERE m.id=:message_id AND {predicate}
                        ORDER BY m.created_at DESC LIMIT 1
                        """
                    ),
                    params,
                )
                row = result.mappings().first()
                if row is None:
                    return None
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        action="message_phone_decrypt",
                        object_type="sms_message",
                        object_id=str(message_id),
                        ip=ip,
                        after={"count": 1, "batch_no": str(row["batch_no"])},
                    ),
                )
                return (
                    bytes(row["phone_enc"]),
                    int(row["key_version"]),
                    str(row["phone_hmac"]),
                )
        finally:
            await engine.dispose()
