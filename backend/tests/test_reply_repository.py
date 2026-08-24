from __future__ import annotations

import base64
from typing import Any, cast

import pytest

from app.services.crypto import CryptoService
from app.services.reply_ingest import (
    ProtectedReply,
    ReplyIngestService,
    ReplyRepository,
)
from app.services.reply_repository import SqlReplyRepository
from app.vendor.zhihui import RawPulledPayload


class FakeResult:
    def __init__(self, scalar: object = None) -> None:
        self.scalar = scalar

    def scalar_one(self) -> object:
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def bind(repository: SqlReplyRepository, connection: FakeConnection) -> FakeEngine:
    engine = FakeEngine(connection)
    repository._engine = lambda: engine  # type: ignore[method-assign]
    return engine


def protected_reply() -> ProtectedReply:
    key = base64.b64encode(b"p" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)

    class Gateway:
        async def get_reply_raw(self) -> RawPulledPayload:
            return RawPulledPayload(b"", 200)

    service = ReplyIngestService(Gateway(), cast(ReplyRepository, object()), crypto)
    return service._parse(
        {
            "taskId": "task-1",
            "customId": "custom1",
            "phone": "13800138000",
            "extCode": "01",
            "contents": "TD",
            "replyTime": "2026-07-12T08:00:00+08:00",
        }
    )


@pytest.mark.asyncio
async def test_raw_reply_is_persisted_in_independent_transaction() -> None:
    repository = SqlReplyRepository()
    connection = FakeConnection([FakeResult(19)])
    engine = bind(repository, connection)

    assert await repository.persist_raw(
        payload_enc=b"cipher",
        payload_sha256="a" * 64,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        custom_ids=["custom-1"],
        item_count=1,
    ) == 19

    sql, params = connection.calls[0]
    assert "raw_vendor_log" in sql
    assert "'reply'" in sql
    assert "processing_started_at" in sql
    assert "processing_lease_id" in sql
    assert "http_status" in sql
    assert "content_encoding" in sql
    assert "capture_state" in sql
    assert "now()" in sql
    assert params["custom_ids"] == ["custom-1"]
    assert params["capture_state"] == "complete"
    assert params["parse_state"] == "unattempted"
    assert params["replay_eligibility"] == "automatic"
    assert engine.disposed


@pytest.mark.asyncio
async def test_reply_insert_is_database_idempotent_and_associates_custom_before_task() -> None:
    repository = SqlReplyRepository()
    connection = FakeConnection([FakeResult()])
    engine = bind(repository, connection)
    reply = protected_reply()

    await repository.store_reply(19, reply)

    sql, params = connection.calls[0]
    normalized_sql = " ".join(sql.split())
    assert "INSERT INTO reply_event" in normalized_sql
    assert "ON CONFLICT(event_key) DO NOTHING" in normalized_sql
    assert "CAST(:match_custom_id AS varchar(32)) IS NOT NULL" in normalized_sql
    assert "c.custom_id=CAST(:match_custom_id AS varchar(32))" in normalized_sql
    assert "c.vendor_task_id=CAST(:vendor_task_id AS varchar(64))" in normalized_sql
    assert "m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))" in normalized_sql
    assert "CAST(:phone_hmac AS char(64))" in normalized_sql
    assert "CAST(:content_enc AS bytea)" in normalized_sql
    assert "is_optout" in normalized_sql
    assert params["is_optout"] is True
    assert "CAST(:reply_time AS timestamptz)" in normalized_sql
    assert (
        "CASE WHEN c.custom_id=CAST(:match_custom_id AS varchar(32)) THEN 0 ELSE 1 END"
        in normalized_sql
    )
    assert "FROM event_insert" in normalized_sql
    assert params["raw_id"] == 19
    assert params["phone_enc"] == reply.phone_enc
    assert params["phone_hmac"] == reply.phone_hmac
    assert params["phone_hmacs"] == list(reply.phone_hmacs)
    assert params["dedup_key_version"] == reply.dedup_key_version
    assert "13800138000" not in str(params)
    assert engine.disposed


@pytest.mark.asyncio
async def test_raw_processed_and_error_flags_are_explicit() -> None:
    repository = SqlReplyRepository()
    connection = FakeConnection([FakeResult(), FakeResult()])
    bind(repository, connection)
    from uuid import UUID

    from app.services.raw_lease import RawProcessingLease

    processed_lease = RawProcessingLease(19, UUID(int=19), 1)
    error_lease = RawProcessingLease(20, UUID(int=20), 3)
    repository.remember_lease(processed_lease)
    repository.remember_lease(error_lease)

    await repository.mark_processed(19)
    await repository.mark_error(20, "ValueError: reply parsing failed")

    assert "processing_started_at=NULL" in connection.calls[0][0]
    assert "processing_started_at=NULL" in connection.calls[1][0]
    assert "processing_lease_id" in connection.calls[0][0]
    assert connection.calls[0][1] == {
        "id": 19,
        "processed": True,
        "error": None,
        "parse_state": "processed",
        "replay_eligibility": "never",
        "lease_id": str(processed_lease.lease_id),
        "epoch": 1,
        "system_replay_audit_state": None,
    }
    assert connection.calls[1][1] == {
        "id": 20,
        "processed": False,
        "error": "ValueError: reply parsing failed",
        "parse_state": "protocol_invalid",
        "replay_eligibility": "manual",
        "lease_id": str(error_lease.lease_id),
        "epoch": 3,
        "system_replay_audit_state": None,
    }
