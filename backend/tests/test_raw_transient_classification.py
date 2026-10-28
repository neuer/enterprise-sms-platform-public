"""暂态数据库异常必须按异常链分类，且终态 SQL 不得提升 never。"""

from __future__ import annotations

from uuid import UUID

import pytest
from asyncpg.exceptions import InternalClientError
from sqlalchemy.exc import InternalError

from app.services.raw_lease import (
    FENCED_TERMINAL_SQL,
    FENCED_TERMINAL_SYSTEM_AUDIT_SQL,
    RawProcessingLease,
)
from app.services.raw_parse import (
    ELIGIBILITY_AUTOMATIC,
    ELIGIBILITY_MANUAL,
    PARSE_PROTOCOL_INVALID,
    PARSE_TRANSIENT_FAILURE,
    classify_raw_disposition,
    mark_error_column_values,
)
from app.services.reply_repository import SqlReplyRepository
from app.services.report_repository import SqlReportRepository


class QueryCanceledError(Exception):
    """模拟 asyncpg 取消，不依赖真实数据库。"""


class DBAPIError(Exception):
    """模拟 SQLAlchemy 包装后的外层异常。"""


def _canceled() -> DBAPIError:
    wrapped = DBAPIError("statement timeout")
    wrapped.__cause__ = QueryCanceledError("canceling statement due to statement timeout")
    return wrapped


def test_sqlalchemy_cancel_chain_stays_automatic_while_type_name_alone_does_not() -> None:
    summary = "DBAPIError: reply parsing failed"
    with_cause = mark_error_column_values(summary, exc=_canceled())
    assert with_cause == {
        "parse_state": PARSE_TRANSIENT_FAILURE,
        "replay_eligibility": ELIGIBILITY_AUTOMATIC,
    }
    without_cause = mark_error_column_values(summary)
    assert without_cause["parse_state"] == PARSE_PROTOCOL_INVALID
    assert without_cause["replay_eligibility"] == ELIGIBILITY_MANUAL
    assert "statement timeout" not in summary


def test_fenced_terminal_sql_keeps_never_for_both_audit_variants() -> None:
    keeper = "WHEN raw_vendor_log.replay_eligibility='never' THEN 'never'"
    assert keeper in FENCED_TERMINAL_SQL
    assert keeper in FENCED_TERMINAL_SYSTEM_AUDIT_SQL
    assert "system_replay_audit_state" not in FENCED_TERMINAL_SQL
    assert (
        "system_replay_audit_state=:system_replay_audit_state" in FENCED_TERMINAL_SYSTEM_AUDIT_SQL
    )
    assert "processing_lease_id=CAST(:lease_id AS uuid)" in FENCED_TERMINAL_SQL
    assert "processing_lease_epoch=:epoch" in FENCED_TERMINAL_SYSTEM_AUDIT_SQL


def test_driver_internal_client_failure_is_transient_but_generic_internal_error_is_not() -> None:
    driver = InternalClientError("synthetic disconnected protocol state")
    wrapped = InternalError(None, None, driver)
    wrapped.__cause__ = driver
    recovered = classify_raw_disposition(exc=wrapped)
    assert recovered.parse_state == PARSE_TRANSIENT_FAILURE
    assert recovered.replay_eligibility == ELIGIBILITY_AUTOMATIC
    generic = classify_raw_disposition(
        exc=InternalError(None, None, Exception("internal SQL error"))
    )
    assert generic.replay_eligibility == ELIGIBILITY_MANUAL


@pytest.mark.parametrize("http_status,encoding", [(503, "identity"), (200, "gzip")])
def test_driver_failure_cannot_override_non_replayable_wire_facts(
    http_status: int, encoding: str
) -> None:
    result = classify_raw_disposition(
        capture_state="complete",
        http_status=http_status,
        content_encoding=encoding,
        exc=InternalClientError("synthetic disconnected protocol state"),
    )
    assert result.replay_eligibility == "never"


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement: object, params: dict[str, object]) -> object:
        self.calls.append((str(statement), params))
        return self

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.disposed = False

    def begin(self) -> _Connection:
        return self.connection

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_mark_error_forwards_exception_chain_without_writing_it() -> None:
    canceled = _canceled()
    lease = RawProcessingLease(7, UUID(int=7), 2)
    for repository in (SqlReplyRepository(), SqlReportRepository()):
        connection = _Connection()
        engine = _Engine(connection)
        repository._engine = lambda engine=engine: engine  # type: ignore[method-assign]
        repository.remember_lease(lease)
        await repository.mark_error(7, "DBAPIError: reply parsing failed", exc=canceled)
        _sql, params = connection.calls[-1]
        assert "WHEN raw_vendor_log.replay_eligibility='never' THEN 'never'" in _sql
        assert params["replay_eligibility"] == ELIGIBILITY_AUTOMATIC
        assert params["parse_state"] == PARSE_TRANSIENT_FAILURE
        assert "statement timeout" not in str(params["error"])
        assert engine.disposed
