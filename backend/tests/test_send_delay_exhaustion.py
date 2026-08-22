from __future__ import annotations

import pytest

import app.tasks.send_repository as module  # noqa: E402
from app.tasks.send_repository import SqlChunkStore  # noqa: E402


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object = None,
        row: dict[str, object] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.scalar = scalar
        self.row = row
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def mappings(self) -> FakeResult:
        return self

    def one(self) -> dict[str, object]:
        assert self.row is not None
        return self.row


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        if not self.results:
            raise AssertionError(f"unexpected SQL: {statement}")
        return self.results.pop(0)


class Context:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class Engine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def begin(self) -> Context:
        return Context(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def store_for(connection: FakeConnection) -> SqlChunkStore:
    store = object.__new__(SqlChunkStore)
    store._engine = lambda: Engine(connection)  # type: ignore[method-assign]
    return store


@pytest.mark.asyncio
async def test_exhaustion_finalizes_messages_batch_and_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(scalar=None),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(row={"id": 11, "status": "completed"}),
        ]
    )
    settled: list[tuple[int, str]] = []
    callbacks: list[int] = []

    async def settle(_connection: object, chunk_id: int, state: str) -> int:
        settled.append((chunk_id, state))
        return 1

    async def callback(_connection: object, batch_id: int) -> None:
        callbacks.append(batch_id)

    monkeypatch.setattr(module, "settle_live_test_attempt", settle)
    monkeypatch.setattr(module, "enqueue_batch_finished", callback)
    await store_for(connection).delay(7, 1011, 1800)

    sql = "\n".join(item[0] for item in connection.calls)
    assert "SELECT batch_id FROM sms_chunk" in sql
    assert "SELECT id FROM sms_batch" in sql and "FOR UPDATE" in sql
    assert "RETURNING batch_id" in sql
    assert "retry_not_before=NULL" in sql
    assert "UPDATE sms_message SET status='failed'" in sql
    assert "UPDATE sms_batch" in sql
    assert settled == [(7, "released")]
    assert callbacks == [11]


@pytest.mark.asyncio
async def test_race_loser_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection([FakeResult(scalar=None), FakeResult(scalar=None)])
    effects: list[object] = []

    async def unexpected(*args: object, **kwargs: object) -> int:
        effects.append((args, kwargs))
        return 0

    monkeypatch.setattr(module, "settle_live_test_attempt", unexpected)
    monkeypatch.setattr(module, "enqueue_batch_finished", unexpected)
    await store_for(connection).delay(7, 1011, 1800)

    assert len(connection.calls) == 2
    assert effects == []


@pytest.mark.asyncio
async def test_non_exhausted_retry_semantics_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection([FakeResult(scalar="market")])
    settled: list[tuple[int, str]] = []
    enqueued: list[tuple[int, str, int]] = []

    async def settle(_connection: object, chunk_id: int, state: str) -> int:
        settled.append((chunk_id, state))
        return 1

    async def enqueue(chunk_id: int, lane: str, countdown: int) -> None:
        enqueued.append((chunk_id, lane, countdown))

    store = store_for(connection)
    monkeypatch.setattr(module, "settle_live_test_attempt", settle)
    monkeypatch.setattr(store, "_enqueue_retry", enqueue)
    await store.delay(7, 1011, 1800)

    assert settled == [(7, "released")]
    assert enqueued == [(7, "bulk", 1800)]


@pytest.mark.asyncio
async def test_race_after_batch_lock_does_not_duplicate_terminal_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(scalar=None),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(scalar=None),
        ]
    )
    effects: list[object] = []

    async def unexpected(*args: object, **kwargs: object) -> int:
        effects.append((args, kwargs))
        return 0

    monkeypatch.setattr(module, "settle_live_test_attempt", unexpected)
    monkeypatch.setattr(module, "enqueue_batch_finished", unexpected)
    await store_for(connection).delay(7, 1011, 1800)

    assert len(connection.calls) == 4
    assert effects == []


@pytest.mark.asyncio
async def test_exhaustion_keeps_batch_sending_when_other_messages_are_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(scalar=None),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(row={"id": 11, "status": "sending"}),
        ]
    )
    callbacks: list[int] = []

    async def settle(_connection: object, _chunk_id: int, _state: str) -> int:
        return 1

    async def callback(_connection: object, batch_id: int) -> None:
        callbacks.append(batch_id)

    monkeypatch.setattr(module, "settle_live_test_attempt", settle)
    monkeypatch.setattr(module, "enqueue_batch_finished", callback)
    await store_for(connection).delay(7, 1011, 1800)

    assert callbacks == []
