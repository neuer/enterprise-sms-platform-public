"""HMAC 黑名单管理与仅含 phone_hmac 的 Redis SET 缓存。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.core.sensitive_text import reject_phone_in_text
from app.services.crypto import PHONE_PATTERN, CryptoService

BLACKLIST_KEY = "blacklist:phone_hmacs"
BLACKLIST_LOADED_KEY = "blacklist:phone_hmacs:loaded"
VALID_SOURCES = frozenset({"manual", "reply_optout", "import"})
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    phone_hmac: str
    phone_enc: bytes
    phone_mask: str
    key_version: int
    source: str
    remark: str | None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BlacklistPage:
    total: int
    items: list[BlacklistEntry]


@dataclass(frozen=True, slots=True)
class BlacklistUpsertResult:
    """批量加黑结果：added 为新增号码数，updated 为已存在并更新来源/备注的号码数。"""

    added: int
    updated: int


@dataclass(frozen=True, slots=True)
class BlacklistAddResult:
    entries: list[BlacklistEntry]
    added: int
    updated: int


class BlacklistRepository(Protocol):
    async def list_page(
        self,
        *,
        source: str | None,
        keyword: str | None,
        page: int,
        size: int,
    ) -> BlacklistPage: ...

    async def all_hmacs(self) -> set[str]: ...

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        actor: str,
        source: str,
    ) -> BlacklistUpsertResult: ...

    async def delete(self, phone_hmac: str, *, actor: str) -> bool: ...


class BlacklistCache(Protocol):
    async def invalidate(self) -> None: ...

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]: ...


class RedisBlacklistCache:
    """以 loaded 标记区分空集合与尚未装载，Redis 丢数据后自动回源。"""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def invalidate(self) -> None:
        await self.redis.delete(BLACKLIST_LOADED_KEY)

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]:
        if not candidates:
            return set()
        if not await self.redis.get(BLACKLIST_LOADED_KEY):
            members = await loader()
            pipe = self.redis.pipeline(transaction=True)
            pipe.delete(BLACKLIST_KEY)
            if members:
                pipe.sadd(BLACKLIST_KEY, *members)
            pipe.set(BLACKLIST_LOADED_KEY, "1")
            await pipe.execute()
        flags = await self.redis.smismember(BLACKLIST_KEY, list(candidates))
        return {value for value, present in zip(candidates, flags, strict=True) if present}


class BlacklistService:
    def __init__(
        self,
        repository: BlacklistRepository,
        cache: BlacklistCache,
        crypto: CryptoService,
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.crypto = crypto

    async def list_page(
        self,
        *,
        source: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> BlacklistPage:
        """分页查询黑名单；keyword 模糊匹配掩码或备注，永不明文。"""
        if source is not None and source not in VALID_SOURCES:
            raise ValueError("invalid blacklist source")
        if page < 1 or not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError("invalid pagination")
        keyword = (keyword or "").strip() or None
        return await self.repository.list_page(
            source=source,
            keyword=keyword,
            page=page,
            size=size,
        )

    async def matches(self, candidates: set[str]) -> set[str]:
        return await self.cache.matches(candidates, self.repository.all_hmacs)

    async def add(
        self,
        phones: list[str],
        *,
        source: str,
        remark: str | None,
        actor: str,
    ) -> BlacklistAddResult:
        """批量加黑；先统一校验号码格式，报错只带行号、不带号码明文。"""
        reject_phone_in_text(remark, field_name="remark")
        if source not in VALID_SOURCES:
            raise ValueError("invalid blacklist source")
        if not phones:
            raise ValueError("phones must not be empty")
        invalid_lines = [
            str(index)
            for index, phone in enumerate(phones, start=1)
            if PHONE_PATTERN.fullmatch(phone) is None
        ]
        if invalid_lines:
            shown = "、".join(invalid_lines[:5])
            suffix = f" 等共 {len(invalid_lines)}" if len(invalid_lines) > 5 else ""
            raise ValueError(f"第 {shown}{suffix} 行号码格式错误，应为 11 位手机号")
        protected = {item.phone_hmac: item for item in map(self.crypto.protect_phone, phones)}
        entries = [
            BlacklistEntry(
                item.phone_hmac,
                item.phone_enc,
                item.phone_mask,
                item.key_version,
                source,
                remark,
            )
            for item in protected.values()
        ]
        try:
            outcome = await self.repository.upsert_many(entries, actor=actor, source=source)
        finally:
            await self.cache.invalidate()
        return BlacklistAddResult(entries, outcome.added, outcome.updated)

    async def delete(self, phone_hmac: str, *, actor: str) -> bool:
        try:
            return await self.repository.delete(phone_hmac, actor=actor)
        finally:
            await self.cache.invalidate()
