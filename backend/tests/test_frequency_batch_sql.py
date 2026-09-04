"""万级频控主体解析必须是块级 SQL，且同一号码只解析一次。"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

import pytest

from app.services.usage_ledger import (
    FrequencyDecisionItem,
    UsageReservationConflict,
    _ensure_frequency_subject,
    _ensure_frequency_subjects_many,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def decision_item(label: str, *, versions: int = 1) -> FrequencyDecisionItem:
    aliases = {version: digest(f"{label}:{version}") for version in range(1, versions + 1)}
    return FrequencyDecisionItem(aliases[versions], aliases)


class _Result:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = rows or []

    def mappings(self) -> list[dict[str, object]]:
        return self._rows


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.aliases: dict[str, UUID] = {}
        self.subjects: dict[UUID, str] = {}

    async def execute(self, statement: object, parameters: dict[str, object] | None = None):
        sql = str(statement)
        self.statements.append(sql)
        params = parameters or {}
        if "FROM usage_frequency_alias" in sql and "SELECT phone_hmac" in sql:
            wanted = set(params["digests"])
            return _Result(
                [
                    {"phone_hmac": hmac, "subject_id": subject_id}
                    for hmac, subject_id in self.aliases.items()
                    if hmac in wanted
                ]
            )
        if "FROM usage_frequency_subject" in sql and "SELECT id,projection_hmac" in sql:
            wanted = set(params["subject_ids"])
            return _Result(
                [
                    {"id": subject_id, "projection_hmac": hmac}
                    for subject_id, hmac in self.subjects.items()
                    if subject_id in wanted
                ]
            )
        if "INSERT INTO usage_frequency_subject" in sql:
            for row in json.loads(str(params["subjects"])):
                self.subjects[UUID(str(row["id"]))] = str(row["projection_hmac"])
            return _Result()
        if "INSERT INTO usage_frequency_alias" in sql:
            for row in json.loads(str(params["aliases"])):
                hmac = str(row["phone_hmac"])
                if hmac not in self.aliases:
                    self.aliases[hmac] = UUID(str(row["subject_id"]))
            return _Result()
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_new_subjects_use_bounded_sql_and_do_not_call_per_number_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_calls = {"count": 0}
    original = _ensure_frequency_subject

    async def wrapped(connection: object, item: FrequencyDecisionItem):
        ensure_calls["count"] += 1
        return await original(connection, item)

    monkeypatch.setattr(
        "app.services.usage_ledger._ensure_frequency_subject", wrapped
    )

    small = [decision_item(f"new-{index}") for index in range(20)]
    large = [decision_item(f"new-{index}") for index in range(80)]
    small_connection = RecordingConnection()
    large_connection = RecordingConnection()

    small_resolved, small_merged = await _ensure_frequency_subjects_many(
        small_connection, small
    )
    large_resolved, large_merged = await _ensure_frequency_subjects_many(
        large_connection, large
    )

    assert ensure_calls["count"] == 0
    assert small_merged == ()
    assert large_merged == ()
    assert len(small_resolved) == 20
    assert len(large_resolved) == 80
    assert [item.phone_hmac for item in small_resolved] == [item.phone_hmac for item in small]
    assert len(small_connection.statements) == len(large_connection.statements)
    assert len(small_connection.statements) <= 6
    assert all(
        item.subject_id in small_connection.subjects for item in small_resolved
    )


@pytest.mark.asyncio
async def test_existing_subjects_are_reused_with_one_alias_lookup_chunk() -> None:
    items = [decision_item(f"old-{index}", versions=2) for index in range(40)]
    connection = RecordingConnection()
    for item in items:
        subject_id = uuid4()
        connection.subjects[subject_id] = item.hmac_aliases[1]
        for digest in item.hmac_aliases.values():
            connection.aliases[digest] = subject_id

    resolved, merged = await _ensure_frequency_subjects_many(connection, items)

    assert merged == ()
    assert len(resolved) == 40
    assert {item.subject_id for item in resolved} == set(connection.subjects)
    assert sum("FROM usage_frequency_alias" in sql for sql in connection.statements) == 2
    assert sum("FROM usage_frequency_subject" in sql for sql in connection.statements) == 1
    assert len(connection.statements) <= 6


@pytest.mark.asyncio
async def test_alias_conflict_fail_closed_when_bind_loses_the_race() -> None:
    item = decision_item("race")
    connection = RecordingConnection()
    winner = uuid4()
    connection.subjects[winner] = digest("other")
    connection.aliases[item.phone_hmac] = winner

    class RacingConnection(RecordingConnection):
        async def execute(self, statement: object, parameters: dict[str, object] | None = None):
            sql = str(statement)
            if "INSERT INTO usage_frequency_subject" in sql:
                result = await super().execute(statement, parameters)
                # 并发请求已把同一 HMAC 绑到另一个主体。
                self.aliases[item.phone_hmac] = winner
                return result
            return await super().execute(statement, parameters)

    racing = RacingConnection()
    with pytest.raises(UsageReservationConflict, match="alias write conflict"):
        await _ensure_frequency_subjects_many(racing, [item])


@pytest.mark.asyncio
async def test_multi_version_aliases_of_one_number_resolve_to_one_subject() -> None:
    item = decision_item("rotate", versions=3)
    connection = RecordingConnection()
    resolved, merged = await _ensure_frequency_subjects_many(connection, [item])
    assert merged == ()
    assert len(resolved) == 1
    assert resolved[0].subject_id in connection.subjects
    assert set(connection.aliases) == set(item.hmac_aliases.values())
    assert len(set(connection.aliases.values())) == 1
