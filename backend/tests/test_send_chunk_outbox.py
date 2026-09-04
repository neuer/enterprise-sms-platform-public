from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.services.outbox import OutboxClaim
from app.tasks import send as send_module
from app.tasks.send import (
    ChunkPayload,
    ChunkTaskResult,
    SendQueuePaused,
    SubmitOutcome,
)


class _Gateway:
    async def aclose(self) -> None:
        return None


class _Worker:
    def __init__(self, submitted: list[tuple[object, str]]) -> None:
        self.submitted = submitted

    async def submit(self, chunk: object, *, lane: str) -> SubmitOutcome:
        self.submitted.append((chunk, lane))
        return SubmitOutcome.SUBMITTED


def _payload(chunk_id: int = 8) -> ChunkPayload:
    return ChunkPayload(
        chunk_id=chunk_id,
        batch_id=3,
        custom_id="custom-1",
        phones=("13800138000",),
        content="通知",
        template_id="",
        sign_name="青鸾",
    )


@pytest.mark.asyncio
async def test_process_batch_only_plans_chunks_and_does_not_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[object, str]] = []
    prepared: list[tuple[str, int]] = []

    class Store:
        async def prepare_chunks(self, batch_no: str, batch_size: int) -> tuple[list[int], str]:
            prepared.append((batch_no, batch_size))
            return [8, 9], "bulk"

    async def components() -> tuple[Any, Any, Any, int]:
        return _Worker(submitted), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)

    assert await send_module._process_batch("batch-1") == 2
    assert prepared == [("batch-1", 500)]
    assert submitted == []


@pytest.mark.asyncio
async def test_process_chunk_decrypts_only_current_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[object, str]] = []
    loaded: list[int] = []
    payload = _payload(8)

    class Store:
        async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
            loaded.append(chunk_id)
            assert chunk_id == 8
            return payload, "realtime"

        async def is_paused(self, lane: str) -> bool:
            assert lane == "realtime"
            return False

    async def components() -> tuple[Any, Any, Any, int]:
        return _Worker(submitted), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)

    assert await send_module._process_chunk(8) == ChunkTaskResult(
        1, SubmitOutcome.SUBMITTED
    )
    assert loaded == [8]
    assert submitted == [(payload, "realtime")]


@pytest.mark.asyncio
async def test_process_chunk_skips_already_claimed_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[object, str]] = []

    class Store:
        async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
            return None

        async def is_paused(self, lane: str) -> bool:
            raise AssertionError("已提交分片不得再检查暂停")

    async def components() -> tuple[Any, Any, Any, int]:
        return _Worker(submitted), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)

    assert await send_module._process_chunk(8) == ChunkTaskResult(
        0, SubmitOutcome.NO_OP_ALREADY_TERMINAL
    )
    assert submitted == []


@pytest.mark.asyncio
async def test_chunk_outbox_duplicate_claim_returns_no_op_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = uuid4()

    class Repository:
        async def claim_execution(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(send_module, "SqlOutboxRepository", lambda: Repository())
    result = await send_module._process_chunk_event(8, str(event_id))
    assert result == ChunkTaskResult(1, SubmitOutcome.NO_OP_ALREADY_TERMINAL)


@pytest.mark.asyncio
async def test_chunk_outbox_pause_fails_closed_and_does_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    event_id = uuid4()

    class Store:
        async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
            return _payload(chunk_id), "realtime"

        async def is_paused(self, lane: str) -> bool:
            return True

    class Repository:
        async def claim_execution(self, claimed_id: object, *, lease_seconds: int) -> OutboxClaim:
            return OutboxClaim(event_id, uuid4(), "chunk.ready", (8,))

        async def heartbeat(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def complete(self, *_args: object) -> None:
            events.append("complete")

        async def fail_execution(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            events.append(str(_args[-1]) if _args else "failed")

    async def components() -> tuple[Any, Any, Any, int]:
        return _Worker([]), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)
    monkeypatch.setattr(send_module, "SqlOutboxRepository", lambda: Repository())

    with pytest.raises(SendQueuePaused):
        await send_module._process_chunk_event(8, str(event_id))

    assert "complete" not in events
    assert "SendQueuePaused" in events


@pytest.mark.asyncio
async def test_chunk_outbox_submit_paused_fails_closed_and_does_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    event_id = uuid4()

    class Store:
        async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
            return _payload(chunk_id), "realtime"

        async def is_paused(self, lane: str) -> bool:
            return False

    class PausedWorker:
        async def submit(self, chunk: object, *, lane: str) -> SubmitOutcome:
            assert lane == "realtime"
            return SubmitOutcome.PAUSED

    class Repository:
        async def claim_execution(self, claimed_id: object, *, lease_seconds: int) -> OutboxClaim:
            return OutboxClaim(event_id, uuid4(), "chunk.ready", (8,))

        async def heartbeat(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def complete(self, *_args: object) -> None:
            events.append("complete")

        async def fail_execution(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            events.append(str(_args[-1]) if _args else "failed")

    async def components() -> tuple[Any, Any, Any, int]:
        return PausedWorker(), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)
    monkeypatch.setattr(send_module, "SqlOutboxRepository", lambda: Repository())

    with pytest.raises(SendQueuePaused):
        await send_module._process_chunk_event(8, str(event_id))

    assert "complete" not in events
    assert "SendQueuePaused" in events


@pytest.mark.asyncio
async def test_legacy_process_chunk_still_returns_zero_when_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[object, str]] = []

    class Store:
        async def load_chunk(self, chunk_id: int) -> tuple[ChunkPayload, str] | None:
            return _payload(chunk_id), "bulk"

        async def is_paused(self, lane: str) -> bool:
            return True

    async def components() -> tuple[Any, Any, Any, int]:
        return _Worker(submitted), Store(), _Gateway(), 500

    monkeypatch.setattr(send_module, "_components", components)

    assert await send_module._process_chunk(8) == ChunkTaskResult(
        0, SubmitOutcome.PAUSED
    )
    assert submitted == []
