"""敏感词 Aho-Corasick 自动机快照。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import ahocorasick

SENSITIVE_WORD_REVISION_KEY = "__sensitive_word_revision"
MAX_PAGE_SIZE = 100
MAX_WORD_LENGTH = 64


@dataclass(frozen=True, slots=True)
class SensitiveWord:
    id: int
    word: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SensitiveWordPage:
    total: int
    items: list[SensitiveWord]


@dataclass(frozen=True, slots=True)
class SensitiveWordAddResult:
    """批量加词结果：created 为新增词条，skipped 为词库已存在而跳过的数量。"""

    created: list[SensitiveWord]
    skipped: int


class SensitiveWordRepository(Protocol):
    async def list_page(
        self,
        *,
        keyword: str | None,
        page: int,
        size: int,
    ) -> SensitiveWordPage: ...

    async def all_words(self) -> list[str]: ...

    async def current_revision(self) -> int: ...

    async def add_many(self, words: list[str], *, actor: str) -> SensitiveWordAddResult: ...

    async def delete(self, word_id: int, *, actor: str) -> bool: ...


class SensitiveWordMatcher:
    """先完整构建新自动机再替换引用，读取方不会观察到半成品。"""

    def __init__(self, words: list[str] | None = None) -> None:
        self._automaton: Any
        self.replace(words or [])

    def replace(self, words: list[str]) -> None:
        normalized = sorted({word.strip() for word in words if word.strip()})
        if not normalized:
            self._automaton = None
            return
        automaton: Any = ahocorasick.Automaton()
        for word in normalized:
            automaton.add_word(word, word)
        automaton.make_automaton()
        self._automaton = automaton

    def hits(self, content: str) -> list[str]:
        if self._automaton is None:
            return []
        return list(dict.fromkeys(word for _, word in self._automaton.iter(content)))


class SensitiveWordIndex:
    """进程内共享快照，首次使用从 PostgreSQL 装载。"""

    def __init__(self) -> None:
        self.matcher = SensitiveWordMatcher()
        self.loaded = False
        self.revision: int | None = None
        self.lock = asyncio.Lock()

    async def match(
        self,
        content: str,
        loader: Callable[[], Awaitable[list[str]]],
        revision_loader: Callable[[], Awaitable[int]] | None = None,
    ) -> list[str]:
        revision = await revision_loader() if revision_loader is not None else None
        if not self.loaded or (revision is not None and revision != self.revision):
            async with self.lock:
                if revision_loader is not None:
                    revision = await revision_loader()
                if not self.loaded or (revision is not None and revision != self.revision):
                    self.matcher.replace(await loader())
                    self.loaded = True
                    self.revision = revision
        return self.matcher.hits(content)

    async def replace(self, words: list[str], *, revision: int | None = None) -> None:
        matcher = SensitiveWordMatcher(words)
        async with self.lock:
            self.matcher = matcher
            self.loaded = True
            self.revision = revision


class SensitiveWordManager:
    def __init__(self, repository: SensitiveWordRepository, index: SensitiveWordIndex) -> None:
        self.repository = repository
        self.index = index

    async def list_page(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> SensitiveWordPage:
        """分页查询词库；keyword 模糊匹配词面。"""
        if page < 1 or not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError("invalid pagination")
        keyword = (keyword or "").strip() or None
        return await self.repository.list_page(keyword=keyword, page=page, size=size)

    async def add(self, words: list[str], *, actor: str) -> SensitiveWordAddResult:
        """批量加词；先校验长度，报错只带行号，写库后整体重建自动机快照。"""
        normalized = list(dict.fromkeys(word.strip() for word in words if word.strip()))
        if not normalized:
            raise ValueError("敏感词不能为空")
        oversized = [
            str(index)
            for index, word in enumerate(words, start=1)
            if len(word.strip()) > MAX_WORD_LENGTH
        ]
        if oversized:
            shown = "、".join(oversized[:5])
            suffix = f" 等共 {len(oversized)}" if len(oversized) > 5 else ""
            raise ValueError(f"第 {shown}{suffix} 行敏感词超过 {MAX_WORD_LENGTH} 字")
        result = await self.repository.add_many(normalized, actor=actor)
        revision = await self.repository.current_revision()
        await self.index.replace(
            await self.repository.all_words(),
            revision=revision,
        )
        return result

    async def delete(self, word_id: int, *, actor: str) -> bool:
        removed = await self.repository.delete(word_id, actor=actor)
        if removed:
            revision = await self.repository.current_revision()
            await self.index.replace(
                await self.repository.all_words(),
                revision=revision,
            )
        return removed


sensitive_word_index = SensitiveWordIndex()
