"""敏感词 Aho-Corasick 自动机快照。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import ahocorasick

SENSITIVE_WORD_REVISION_KEY = "__sensitive_word_revision"


@dataclass(frozen=True, slots=True)
class SensitiveWord:
    id: int
    word: str


class SensitiveWordRepository(Protocol):
    async def list_words(self) -> list[SensitiveWord]: ...

    async def all_words(self) -> list[str]: ...

    async def current_revision(self) -> int: ...

    async def add_many(self, words: list[str], *, actor: str) -> list[SensitiveWord]: ...

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

    async def list_words(self) -> list[SensitiveWord]:
        return await self.repository.list_words()

    async def add(self, words: list[str], *, actor: str) -> list[SensitiveWord]:
        normalized = list(dict.fromkeys(word.strip() for word in words if word.strip()))
        if not normalized:
            raise ValueError("敏感词不能为空")
        if any(len(word) > 64 for word in normalized):
            raise ValueError("敏感词长度不能超过64")
        created = await self.repository.add_many(normalized, actor=actor)
        revision = await self.repository.current_revision()
        await self.index.replace(
            await self.repository.all_words(),
            revision=revision,
        )
        return created

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
