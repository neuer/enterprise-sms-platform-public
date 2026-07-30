from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.tasks.send_repository as send_repository_module
from app.services.vendor_test_budget import SubmissionClaimStatus
from app.tasks import celery_app
from app.tasks.send import ChunkPayload
from app.tasks.send_repository import SqlChunkStore


class FakeResult:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        *,
        scalar: object = None,
        scalars: list[object] | None = None,
        rows: list[dict[str, object]] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.row = row
        self.scalar = scalar
        self.scalar_values = scalars or []
        self.rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> FakeResult:
        return self

    def one(self) -> dict[str, object]:
        assert self.row is not None
        return self.row

    def one_or_none(self) -> dict[str, object] | None:
        return self.row

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def scalar_one(self) -> object:
        return self.scalar

    def scalars(self) -> list[object]:
        return self.scalar_values

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[str] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append(str(statement))
        if len(self.calls) > 1:
            raise AssertionError("非 queued 批次不得查询或创建分片")
        return FakeResult({"id": 7, "category": "market", "status": self.status})


class SequenceConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def chunk_store() -> SqlChunkStore:
    return SqlChunkStore(
        object(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
            ),
        ),
        redis=object(),
    )


@pytest.mark.asyncio
async def test_payload_carries_persisted_retry_count() -> None:
    class FakeCrypto:
        def decrypt_phone(
            self,
            _ciphertext: bytes,
            _key_version: int,
            _phone_hmac: str,
        ) -> str:
            return "13800138000"

        def decrypt_bound_packed_text(self, _ciphertext: bytes, _context: object) -> str:
            return "通知"

    store = SqlChunkStore(
        FakeCrypto(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
            ),
        ),
        redis=object(),
    )
    connection = SequenceConnection(
        [
            FakeResult(
                {
                    "chunk_id": 7,
                    "batch_id": 3,
                    "batch_no": "batch-1",
                    "custom_id": "custom-1 ",
                    "retry_count": 4,
                    "send_content_enc": b"content",
                    "sign_name": "青鸾",
                    "vendor_template_id": None,
                }
            ),
            FakeResult(
                rows=[
                    {
                        "phone_enc": b"phone",
                        "phone_hmac": "a" * 64,
                        "key_version": 1,
                    }
                ]
            ),
        ]
    )

    payload = await store._payload(connection, 7)  # type: ignore[arg-type]

    assert payload.retry_count == 4
    assert "c.retry_count" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_payload_does_not_apply_live_test_recipient_guard_in_mock_mode() -> None:
    class FakeCrypto:
        def decrypt_phone(
            self,
            _ciphertext: bytes,
            _key_version: int,
            _phone_hmac: str,
        ) -> str:
            return "13800138000"

        def decrypt_bound_packed_text(self, _ciphertext: bytes, _context: object) -> str:
            return "通知"

    store = SqlChunkStore(
        FakeCrypto(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
                vendor_live_test=False,
            ),
        ),
        redis=object(),
    )
    connection = SequenceConnection(
        [
            FakeResult(
                {
                    "chunk_id": 7,
                    "batch_id": 3,
                    "batch_no": "batch-1",
                    "custom_id": "custom-1",
                    "retry_count": 0,
                    "send_content_enc": b"content",
                    "sign_name": "青鸾",
                    "vendor_template_id": None,
                }
            ),
            FakeResult(
                rows=[
                        {
                            "phone_enc": b"phone",
                            "phone_hmac": "a" * 64,
                            "key_version": 1,
                        "recipient_allowed": False,
                    }
                ]
            ),
        ]
    )

    payload = await store._payload(connection, 7)  # type: ignore[arg-type]

    assert payload.phones == ("13800138000",)
    assert payload.denied_recipient_count == 0


@pytest.mark.asyncio
async def test_live_payload_accepts_active_recipient_across_hmac_key_versions() -> None:
    class FakeCrypto:
        def decrypt_phone(
            self,
            _ciphertext: bytes,
            _key_version: int,
            _phone_hmac: str,
        ) -> str:
            return "13900000001"

        def decrypt_bound_packed_text(self, _ciphertext: bytes, _context: object) -> str:
            return "通知"

        def hmac_candidates(self, phone: str) -> dict[int, str]:
            assert phone == "13900000001"
            return {1: "a" * 64, 2: "b" * 64}

    store = SqlChunkStore(
        FakeCrypto(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
                vendor_live_test=True,
            ),
        ),
        redis=object(),
    )
    connection = SequenceConnection(
        [
            FakeResult(),
            FakeResult(
                {
                    "chunk_id": 7,
                    "batch_id": 3,
                    "batch_no": "batch-1",
                    "custom_id": "custom-1",
                    "retry_count": 0,
                    "send_content_enc": b"content",
                    "sign_name": "青鸾",
                    "vendor_template_id": None,
                }
            ),
            FakeResult(
                rows=[
                    {
                        "phone_enc": b"phone",
                        "phone_hmac": "b" * 64,
                        "key_version": 2,
                    }
                ]
            ),
            FakeResult(scalar=True),
        ]
    )

    payload = await store._payload(connection, 7)  # type: ignore[arg-type]

    assert payload.phones == ("13900000001",)
    assert payload.denied_recipient_count == 0
    lock_sql, lock_params = connection.calls[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == {"lock_name": "vendor-test-recipient"}
    guard_sql, guard_params = connection.calls[3]
    assert "vendor_test_recipient" in guard_sql
    assert "status='active'" in guard_sql
    assert set(cast(dict[str, object], guard_params).values()) == {
        1,
        2,
        "a" * 64,
        "b" * 64,
    }


@pytest.mark.asyncio
async def test_live_payload_waits_for_recipient_maintenance_lock_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCrypto:
        def decrypt_phone(
            self,
            _ciphertext: bytes,
            _key_version: int,
            _phone_hmac: str,
        ) -> str:
            return "13900000001"

        def decrypt_bound_packed_text(self, _ciphertext: bytes, _context: object) -> str:
            return "通知"

        def hmac_candidates(self, _phone: str) -> dict[int, str]:
            return {1: "a" * 64}

    store = SqlChunkStore(
        FakeCrypto(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
                vendor_live_test=True,
            ),
        ),
        redis=object(),
    )
    connection = SequenceConnection(
        [
            FakeResult(
                {
                    "chunk_id": 7,
                    "batch_id": 3,
                    "batch_no": "batch-1",
                    "custom_id": "custom-1",
                    "retry_count": 0,
                    "send_content_enc": b"content",
                    "sign_name": "青鸾",
                    "vendor_template_id": None,
                }
            ),
            FakeResult(
                rows=[
                    {
                        "phone_enc": b"phone",
                        "phone_hmac": "a" * 64,
                        "key_version": 1,
                    }
                ]
            ),
            FakeResult(scalar=True),
        ]
    )
    lock_started = asyncio.Event()
    release_lock = asyncio.Event()

    async def held_lock(observed_connection: object) -> None:
        assert observed_connection is connection
        lock_started.set()
        await release_lock.wait()

    monkeypatch.setattr(
        send_repository_module,
        "lock_vendor_test_recipient_maintenance",
        held_lock,
    )

    task = asyncio.create_task(store._payload(connection, 7))  # type: ignore[arg-type]
    await lock_started.wait()
    assert connection.calls == []
    release_lock.set()
    payload = await task

    assert payload.phones == ("13900000001",)
    assert "SELECT c.id chunk_id" in connection.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["scheduled", "pending_approval"])
async def test_prepare_chunks_rejects_batch_that_is_not_queued(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    store = chunk_store()
    connection = FakeConnection(status)
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    with pytest.raises(RuntimeError, match="批次状态不允许发送"):
        await store.prepare_chunks("batch-1", 500)

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_prepare_existing_chunks_atomically_moves_queued_batch_to_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult({"id": 7, "category": "market", "status": "queued"}),
            FakeResult(scalars=[8]),
            FakeResult(),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    payload = object()

    async def loaded_payload(*_args: object) -> object:
        return payload

    monkeypatch.setattr(store, "_payload", loaded_payload)

    assert await store.prepare_chunks("batch-1", 500) == ([payload], "bulk")
    assert "retry_not_before<=now()" in connection.calls[1][0]
    assert "status='sending'" in connection.calls[2][0]
    assert connection.calls[2][1] == {"id": 7}


class StatusConnection:
    def __init__(self, batch_status: str) -> None:
        self.batch_status = batch_status
        self.calls: list[str] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append(str(statement))
        return FakeResult(
            {
                "status": "retrying",
                "category": "market",
                "batch_status": self.batch_status,
            }
        )


class StatusEngine(FakeEngine):
    def connect(self) -> FakeContext:
        return FakeContext(self.connection)


@pytest.mark.asyncio
async def test_load_chunk_rejects_retry_when_batch_is_balance_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    monkeypatch.setattr(
        store,
        "_engine",
        lambda: StatusEngine(StatusConnection("balance_blocked")),
    )

    async def unexpected_payload(*_args: object) -> object:
        raise AssertionError("balance_blocked 批次不得解密分片载荷")

    monkeypatch.setattr(store, "_payload", unexpected_payload)

    assert await store.load_chunk(7) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_status", ["queued", "sending"])
async def test_load_chunk_allows_sendable_batch_states(
    monkeypatch: pytest.MonkeyPatch,
    batch_status: str,
) -> None:
    store = chunk_store()
    connection = StatusConnection(batch_status)
    monkeypatch.setattr(
        store,
        "_engine",
        lambda: StatusEngine(connection),
    )
    payload = object()

    async def loaded_payload(*_args: object) -> object:
        return payload

    monkeypatch.setattr(store, "_payload", loaded_payload)

    assert await store.load_chunk(7) == (payload, "bulk")
    assert "retry_not_before<=now()" in connection.calls[0]


class AtomicGateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class AtomicGateConnection:
    def __init__(self, batch_status: str) -> None:
        self.batch_status = batch_status
        self.calls: list[str] = []

    async def execute(self, statement: object, params: object = None) -> AtomicGateResult:
        sql = str(statement)
        self.calls.append(sql)
        has_batch_gate = (
            "EXISTS" in sql
            and "sms_batch" in sql
            and "b.status IN ('queued','sending')" in sql
        )
        if not has_batch_gate:
            return AtomicGateResult(1)
        return AtomicGateResult(1 if self.batch_status in {"queued", "sending"} else 0)


@pytest.mark.asyncio
async def test_mark_submitting_atomically_rejects_balance_blocked_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    monkeypatch.setattr(
        store,
        "_engine",
        lambda: FakeEngine(AtomicGateConnection("balance_blocked")),
    )

    assert await store.mark_submitting(7, 4) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_status", ["queued", "sending"])
async def test_mark_submitting_allows_sendable_batch_states(
    monkeypatch: pytest.MonkeyPatch,
    batch_status: str,
) -> None:
    store = chunk_store()
    connection = AtomicGateConnection(batch_status)
    monkeypatch.setattr(
        store,
        "_engine",
        lambda: FakeEngine(connection),
    )

    assert await store.mark_submitting(7, 4) is True
    assert "UPDATE sms_batch SET updated_at=now()" in connection.calls[0]
    assert "submitting_since=now()" in connection.calls[0]
    assert "retry_not_before<=now()" in connection.calls[0]
    assert "retry_not_before=NULL" in connection.calls[0]
    assert "c.retry_count=:expected_retry_count" in connection.calls[0]


@pytest.mark.asyncio
async def test_claim_reserves_segments_and_marks_submitting_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult({"id": 7, "batch_id": 11}),
            FakeResult(),
            FakeResult(
                {
                    "in_flight_segments": 20,
                    "confirmed_segments": 30,
                    "uncertain_segments": 10,
                }
            ),
            FakeResult(scalar=3),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    monkeypatch.setattr(
        send_repository_module,
        "current_live_test_time",
        lambda: datetime(2026, 7, 16, 8, tzinfo=UTC),
    )

    claim = await store.claim_submission(
        7,
        2,
        5,
        enforce_live_test_budget=True,
    )

    assert claim.status is SubmissionClaimStatus.CLAIMED
    assert "FOR UPDATE" in connection.calls[0][0]
    assert "vendor_test_daily_usage" in connection.calls[1][0]
    assert "FOR UPDATE" in connection.calls[2][0]
    assert "status='submitting'" in connection.calls[3][0]
    assert "vendor_attempt_count=vendor_attempt_count+1" in connection.calls[3][0]
    assert connection.calls[4][1] == {
        "usage_date": datetime(2026, 7, 16, 8, tzinfo=UTC).date(),
        "chunk_id": 7,
        "attempt_no": 3,
        "segments": 5,
    }
    assert "in_flight_segments=in_flight_segments+:segments" in connection.calls[5][0]
    assert "UPDATE sms_batch" in connection.calls[6][0]


@pytest.mark.asyncio
async def test_claim_at_daily_limit_leaves_chunk_sendable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult({"id": 7, "batch_id": 11}),
            FakeResult(),
            FakeResult(
                {
                    "in_flight_segments": 5,
                    "confirmed_segments": 94,
                    "uncertain_segments": 1,
                }
            ),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    monkeypatch.setattr(
        send_repository_module,
        "current_live_test_time",
        lambda: datetime(2026, 7, 16, 15, 59, tzinfo=UTC),
    )

    claim = await store.claim_submission(
        7,
        2,
        1,
        enforce_live_test_budget=True,
    )

    assert claim.status is SubmissionClaimStatus.DAILY_LIMIT
    assert claim.reset_at is not None
    assert claim.reset_at.isoformat() == "2026-07-17T00:00:00+08:00"
    assert len(connection.calls) == 3


class ConcurrentBudgetState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.in_flight = 0


class ConcurrentBudgetConnection:
    def __init__(self, state: ConcurrentBudgetState) -> None:
        self.state = state

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        sql = str(statement)
        values = cast(dict[str, object], params or {})
        if "SELECT c.id,c.batch_id" in sql:
            return FakeResult({"id": values["id"], "batch_id": 11})
        if "SELECT in_flight_segments" in sql:
            return FakeResult(
                {
                    "in_flight_segments": self.state.in_flight,
                    "confirmed_segments": 0,
                    "uncertain_segments": 0,
                }
            )
        if "RETURNING vendor_attempt_count" in sql:
            return FakeResult(scalar=1)
        if "in_flight_segments=in_flight_segments+:segments" in sql:
            self.state.in_flight += int(values["segments"])
        return FakeResult()


class LockedContext:
    def __init__(self, state: ConcurrentBudgetState) -> None:
        self.state = state
        self.connection = ConcurrentBudgetConnection(state)

    async def __aenter__(self) -> ConcurrentBudgetConnection:
        await self.state.lock.acquire()
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        self.state.lock.release()


class ConcurrentBudgetEngine:
    def __init__(self, state: ConcurrentBudgetState) -> None:
        self.state = state

    def begin(self) -> LockedContext:
        return LockedContext(self.state)

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_concurrent_claims_never_make_total_exceed_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConcurrentBudgetState()
    store = chunk_store()
    monkeypatch.setattr(store, "_engine", lambda: ConcurrentBudgetEngine(state))
    monkeypatch.setattr(
        send_repository_module,
        "current_live_test_time",
        lambda: datetime(2026, 7, 16, 8, tzinfo=UTC),
    )

    first, second = await asyncio.gather(
        store.claim_submission(1, 0, 60, enforce_live_test_budget=True),
        store.claim_submission(2, 0, 50, enforce_live_test_budget=True),
    )

    assert {first.status, second.status} == {
        SubmissionClaimStatus.CLAIMED,
        SubmissionClaimStatus.DAILY_LIMIT,
    }
    assert state.in_flight == 60


@pytest.mark.asyncio
async def test_claim_without_live_budget_uses_existing_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()

    async def mark_submitting(chunk_id: int, retry_count: int) -> bool:
        assert (chunk_id, retry_count) == (7, 2)
        return True

    monkeypatch.setattr(store, "mark_submitting", mark_submitting)

    claim = await store.claim_submission(
        7,
        2,
        999,
        enforce_live_test_budget=False,
    )

    assert claim.status is SubmissionClaimStatus.CLAIMED


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        self.set_calls.append((key, value, ex))

    async def eval(self, script: str, numkeys: int, *values: object) -> int:
        self.eval_calls.append((script, numkeys, values))
        keys = values[:numkeys]
        arguments = values[numkeys:]
        assert len(keys) == 2 and arguments == ("vendor-test-agent-stale",)
        for key in keys:
            self.values[str(key)] = str(arguments[0])
        return 1


@pytest.mark.asyncio
async def test_daily_pause_expiry_never_clears_critical_pause() -> None:
    redis = FakeRedis({"queue:paused:realtime": "1010"})
    store = SqlChunkStore(
        object(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
            ),
        ),
        redis=redis,
    )

    await store.pause_daily_limit(
        "realtime",
        datetime(2026, 7, 17, tzinfo=UTC),
        now=datetime(2026, 7, 16, 23, 59, tzinfo=UTC),
    )
    redis.values.pop("queue:paused:vendor-test-daily:realtime")

    assert await store.is_paused("realtime") is True
    assert redis.values["queue:paused:realtime"] == "1010"
    assert redis.set_calls[0][0] == "queue:paused:vendor-test-daily:realtime"
    assert redis.set_calls[0][2] == 60


@pytest.mark.asyncio
async def test_any_historical_partial_agent_stale_key_pauses_both_lanes() -> None:
    redis = FakeRedis(
        {"queue:paused:vendor-test-agent-stale:realtime": "vendor-test-agent-stale"}
    )
    store = SqlChunkStore(
        object(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
            ),
        ),
        redis=redis,
    )

    assert await store.is_paused("realtime") is True
    assert await store.is_paused("bulk") is True


@pytest.mark.asyncio
async def test_agent_stale_pause_uses_independent_named_keys() -> None:
    redis = FakeRedis(
        {
            "queue:paused:realtime": "1010",
            "queue:paused:vendor-test-daily:bulk": "daily_limit",
        }
    )
    store = SqlChunkStore(
        object(),  # type: ignore[arg-type]
        settings=cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                redis_control_url="redis://unused",
            ),
        ),
        redis=redis,
    )

    await store.pause_control_agent_stale()

    assert redis.values["queue:paused:realtime"] == "1010"
    assert redis.values["queue:paused:vendor-test-daily:bulk"] == "daily_limit"
    assert redis.values["queue:paused:vendor-test-agent-stale:realtime"] == (
        "vendor-test-agent-stale"
    )
    assert redis.values["queue:paused:vendor-test-agent-stale:bulk"] == (
        "vendor-test-agent-stale"
    )
    assert redis.set_calls == []
    assert len(redis.eval_calls) == 1
    script, numkeys, values = redis.eval_calls[0]
    assert numkeys == 2
    assert "redis.call('set',KEYS[1],ARGV[1])" in script
    assert values == (
        "queue:paused:vendor-test-agent-stale:realtime",
        "queue:paused:vendor-test-agent-stale:bulk",
        "vendor-test-agent-stale",
    )


@pytest.mark.asyncio
async def test_stale_state_after_claim_releases_attempt_and_keeps_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [FakeResult(scalar=7), FakeResult(rowcount=1)]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    await store.release_control_claim(7)

    sql, params = connection.calls[0]
    assert "status='retrying'" in sql
    assert "status='submitting'" in sql
    assert "uncertain" not in sql
    assert params == {"id": 7}
    assert connection.calls[1][1] == {"chunk_id": 7, "status": "released"}


@pytest.mark.asyncio
async def test_submitted_and_uncertain_transitions_clear_submitting_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [FakeResult(scalar=7), FakeResult(rowcount=1), FakeResult()]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    await store.mark_submitted(7, "task-1")

    assert "submitting_since=NULL" in connection.calls[0][0]
    assert connection.calls[1][1] == {"chunk_id": 7, "status": "confirmed"}

    uncertain_connection = SequenceConnection(
        [FakeResult(scalar=7), FakeResult(rowcount=1)]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(uncertain_connection))
    await store.mark_uncertain(7)

    assert "uncertain_since=COALESCE(submitting_since,now())" in uncertain_connection.calls[0][0]
    assert "submitting_since=NULL" in uncertain_connection.calls[0][0]
    assert uncertain_connection.calls[1][1] == {"chunk_id": 7, "status": "uncertain"}


@pytest.mark.asyncio
async def test_schedule_retry_cas_persists_due_time_and_caps_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [FakeResult(scalar=7), FakeResult(rowcount=1)]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    enqueued: list[tuple[str, list[int], str, int]] = []

    def send_task(
        name: str,
        *,
        args: list[int],
        queue: str,
        countdown: int,
        ignore_result: bool,
    ) -> None:
        assert ignore_result is True
        enqueued.append((name, args, queue, countdown))

    monkeypatch.setattr(celery_app, "send_task", send_task)

    assert await store.schedule_retry(7, 5002, 4, 16) is True
    sql, params = connection.calls[0]
    assert "retry_count=:expected_retry_count" in sql
    assert "retry_count<5" in sql
    assert "retry_not_before" in sql
    assert "make_interval(secs=>:delay_s)" in sql
    assert params == {
        "id": 7,
        "code": 5002,
        "expected_retry_count": 4,
        "delay_s": 16,
    }
    assert connection.calls[1][1] == {"chunk_id": 7, "status": "released"}
    assert enqueued == [("app.tasks.send.process_chunk", [7], "realtime", 16)]


@pytest.mark.asyncio
async def test_long_delay_persists_due_time_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [FakeResult(scalar="market"), FakeResult(rowcount=1)]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    enqueued: list[tuple[str, list[int], str, int]] = []

    def send_task(
        name: str,
        *,
        args: list[int],
        queue: str,
        countdown: int,
        ignore_result: bool,
    ) -> None:
        assert ignore_result is True
        enqueued.append((name, args, queue, countdown))

    monkeypatch.setattr(celery_app, "send_task", send_task)

    await store.delay(7, 1011, 1800)

    sql, params = connection.calls[0]
    assert "retry_not_before=now()+make_interval(secs=>:delay_s)" in sql
    assert "status='submitting'" in sql
    assert "retry_count=retry_count+1" not in sql
    assert params == {"id": 7, "code": 1011, "delay_s": 1800}
    assert connection.calls[1][1] == {"chunk_id": 7, "status": "released"}
    assert enqueued == [("app.tasks.send.process_chunk", [7], "bulk", 1800)]


@pytest.mark.asyncio
async def test_balance_blocked_retry_is_immediately_due_for_admin_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [FakeResult(scalar=7), FakeResult(rowcount=1), FakeResult()]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    await store.balance_blocked(3, 7)

    chunk_sql, chunk_params = connection.calls[0]
    assert "status='retrying'" in chunk_sql
    assert "vendor_code=999" in chunk_sql
    assert "retry_not_before=now()" in chunk_sql
    assert chunk_params == {"id": 7}
    assert connection.calls[1][1] == {"chunk_id": 7, "status": "released"}
    assert "status='balance_blocked'" in connection.calls[2][0]

    due_connection = StatusConnection("queued")
    monkeypatch.setattr(store, "_engine", lambda: StatusEngine(due_connection))
    payload = object()

    async def loaded_payload(*_args: object) -> object:
        return payload

    monkeypatch.setattr(store, "_payload", loaded_payload)
    assert await store.load_chunk(7) == (payload, "bulk")
    assert "retry_not_before<=now()" in due_connection.calls[0]


@pytest.mark.asyncio
async def test_mark_submitted_losing_stale_recovery_race_does_not_mark_messages_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection([FakeResult(scalar=None)])
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    await store.mark_submitted(7, "task-1")

    assert len(connection.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_status", "expected_callbacks"),
    [("sending", []), ("completed", [11])],
)
async def test_mark_failed_aggregates_batch_and_only_enqueues_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    aggregate_status: str,
    expected_callbacks: list[int],
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(scalar=11),
            FakeResult(rowcount=1),
            FakeResult(),
            FakeResult({"id": 11, "status": aggregate_status}),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    callbacks: list[int] = []

    async def enqueue(_connection: object, batch_id: int) -> None:
        callbacks.append(batch_id)

    monkeypatch.setattr(send_repository_module, "enqueue_batch_finished", enqueue)

    await store.mark_failed(7, 1002, "bad content")

    assert callbacks == expected_callbacks
    assert "FROM sms_chunk" in connection.calls[0][0]
    assert "FOR UPDATE" in connection.calls[1][0]
    assert "sms_batch" in connection.calls[1][0]
    assert "status='failed'" in connection.calls[2][0]
    assert "status='submitting'" in connection.calls[2][0]
    assert "submitting_since=NULL" in connection.calls[2][0]
    assert connection.calls[3][1] == {"chunk_id": 7, "status": "released"}
    assert connection.calls[4][1] == {"id": 7}
    aggregate_sql = connection.calls[5][0]
    assert "count(*) FILTER (WHERE status='delivered')" in aggregate_sql
    assert "count(*) FILTER (WHERE status='failed')" in aggregate_sql
    assert "count(*) FILTER (WHERE status='unknown')" in aggregate_sql
    assert "status IN ('pending','sent')" in aggregate_sql
    assert "s.active=0" in aggregate_sql


@pytest.mark.asyncio
async def test_repeated_mark_failed_is_noop_and_does_not_duplicate_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection([FakeResult(scalar=None)])
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    async def unexpected_callback(*_args: object) -> None:
        raise AssertionError("重复终态不得再次创建 callback")

    monkeypatch.setattr(
        send_repository_module,
        "enqueue_batch_finished",
        unexpected_callback,
    )

    await store.mark_failed(7, 1002, "bad content")

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_split_releases_parent_and_preserves_attempt_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult(
                rows=[
                    {"id": 1, "created_at": "a"},
                    {"id": 2, "created_at": "b"},
                    {"id": 3, "created_at": "c"},
                    {"id": 4, "created_at": "d"},
                ]
            ),
            FakeResult(scalar=7),
            FakeResult(rowcount=1),
            FakeResult(scalar=2),
            FakeResult(scalar=20),
            FakeResult(),
            FakeResult(scalar=21),
            FakeResult(),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    async def payload(_connection: object, chunk_id: int) -> ChunkPayload:
        return ChunkPayload(chunk_id, 3, f"child-{chunk_id}", (), "通知", "", "", 0)

    monkeypatch.setattr(store, "_payload", payload)
    parent = ChunkPayload(7, 3, "parent", ("a", "b", "c", "d"), "通知", "", "", 0)

    children = await store.split_once(parent)

    assert [child.chunk_id for child in children] == [20, 21]
    statements = [sql for sql, _params in connection.calls]
    assert not any("DELETE FROM sms_chunk" in sql for sql in statements)
    assert "status='failed'" in statements[1]
    assert "vendor_code=1006" in statements[1]
    assert connection.calls[2][1] == {"chunk_id": 7, "status": "released"}
    first_insert = next(
        index for index, sql in enumerate(statements) if "INSERT INTO sms_chunk" in sql
    )
    assert first_insert > 2


@pytest.mark.asyncio
async def test_guard_denial_fails_unclaimed_chunk_without_budget_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult(scalar=11),
            FakeResult(),
            FakeResult({"id": 11, "status": "completed"}),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    callbacks: list[int] = []

    async def enqueue(_connection: object, batch_id: int) -> None:
        callbacks.append(batch_id)

    monkeypatch.setattr(send_repository_module, "enqueue_batch_finished", enqueue)

    await store.reject_disallowed_recipient(7, 1)

    statements = [sql for sql, _params in connection.calls]
    assert "status IN ('pending','retrying')" in statements[0]
    assert "status='failed'" in statements[2]
    assert "vendor_test_send_attempt" not in " ".join(statements)
    assert connection.calls[2][1] == {
        "id": 7,
        "message": "live-test recipient denied: count=1",
    }
    assert callbacks == [11]


@pytest.mark.asyncio
async def test_daily_limit_defers_unclaimed_chunk_until_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection([FakeResult(scalar="market")])
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))
    enqueued: list[tuple[str, list[int], str, int]] = []

    def send_task(
        name: str,
        *,
        args: list[int],
        queue: str,
        countdown: int,
        ignore_result: bool,
    ) -> None:
        assert ignore_result is True
        enqueued.append((name, args, queue, countdown))

    monkeypatch.setattr(celery_app, "send_task", send_task)
    reset_at = datetime(2026, 7, 17, tzinfo=UTC)
    monkeypatch.setattr(
        send_repository_module,
        "current_live_test_time",
        lambda: datetime(2026, 7, 16, 23, 59, tzinfo=UTC),
    )

    await store.defer_daily_limit(7, "bulk", reset_at)

    sql, params = connection.calls[0]
    assert "status IN ('pending','retrying')" in sql
    assert "retry_not_before=:reset_at" in sql
    assert params == {"id": 7, "reset_at": reset_at}
    assert enqueued == [("app.tasks.send.process_chunk", [7], "bulk", 60)]
