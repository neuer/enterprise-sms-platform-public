from __future__ import annotations

import pytest

from app.services.sensitive import (
    SensitiveWord,
    SensitiveWordIndex,
    SensitiveWordManager,
    SensitiveWordMatcher,
)


def test_aho_matcher_returns_unique_hits_and_atomically_replaces_dictionary() -> None:
    matcher = SensitiveWordMatcher(["赌博", "赌博网站", "诈骗"])
    assert matcher.hits("这是赌博网站诈骗信息") == ["赌博", "赌博网站", "诈骗"]

    matcher.replace(["新词"])
    assert matcher.hits("赌博网站") == []
    assert matcher.hits("包含新词") == ["新词"]


def test_aho_matcher_ignores_blank_and_duplicate_words() -> None:
    matcher = SensitiveWordMatcher(["", "敏感", "敏感", "  "])
    assert matcher.hits("敏感内容") == ["敏感"]


def test_aho_matcher_empty_dictionary_matches_nothing() -> None:
    """空词库是有效初始状态，不得调用未转换 trie 的 iter。"""

    matcher = SensitiveWordMatcher([])
    assert matcher.hits("任意短信内容") == []

    matcher.replace(["敏感"])
    matcher.replace([])
    assert matcher.hits("敏感内容") == []


class FakeRepository:
    def __init__(self) -> None:
        self.words = {1: "旧词"}
        self.audits: list[dict[str, object]] = []
        self.revision = 0

    async def list_words(self) -> list[SensitiveWord]:
        return [SensitiveWord(id_, word) for id_, word in self.words.items()]

    async def all_words(self) -> list[str]:
        return list(self.words.values())

    async def current_revision(self) -> int:
        return self.revision

    async def add_many(self, words: list[str], *, actor: str) -> list[SensitiveWord]:
        start = max(self.words, default=0) + 1
        created = [SensitiveWord(start + index, word) for index, word in enumerate(words)]
        self.words.update({item.id: item.word for item in created})
        if created:
            self.revision += 1
        self.audits.append({"action": "add", "actor": actor, "count": len(created)})
        return created

    async def delete(self, word_id: int, *, actor: str) -> bool:
        removed = self.words.pop(word_id, None) is not None
        if removed:
            self.revision += 1
        self.audits.append({"action": "delete", "actor": actor, "count": int(removed)})
        return removed


@pytest.mark.asyncio
async def test_manager_mutations_rebuild_complete_snapshot_and_audit_count_only() -> None:
    repository = FakeRepository()
    index = SensitiveWordIndex()
    manager = SensitiveWordManager(repository, index)

    created = await manager.add(["新词", " 新词 ", "诈骗"], actor="admin01")
    assert [item.word for item in created] == ["新词", "诈骗"]
    assert await index.match("旧词和新词", repository.all_words) == ["旧词", "新词"]
    assert repository.audits[0] == {"action": "add", "actor": "admin01", "count": 2}

    assert await manager.delete(created[0].id, actor="admin01") is True
    assert await index.match("旧词和新词", repository.all_words) == ["旧词"]


@pytest.mark.asyncio
async def test_index_refreshes_when_another_process_changes_revision() -> None:
    repository = FakeRepository()
    writer_index = SensitiveWordIndex()
    reader_index = SensitiveWordIndex()
    manager = SensitiveWordManager(repository, writer_index)

    assert await reader_index.match(
        "旧词和新词",
        repository.all_words,
        repository.current_revision,
    ) == ["旧词"]

    await manager.add(["新词"], actor="admin01")

    assert await reader_index.match(
        "旧词和新词",
        repository.all_words,
        repository.current_revision,
    ) == ["旧词", "新词"]


@pytest.mark.asyncio
async def test_manager_rejects_empty_or_oversized_words() -> None:
    manager = SensitiveWordManager(FakeRepository(), SensitiveWordIndex())
    with pytest.raises(ValueError):
        await manager.add(["   "], actor="admin01")
    with pytest.raises(ValueError):
        await manager.add(["超" * 65], actor="admin01")
