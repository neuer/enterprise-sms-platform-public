from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.core.sensitive_http import sensitive_http_request
from app.services.pipeline_repository import SqlPipelineStore


@pytest.mark.asyncio
async def test_sensitive_http_scope_is_task_local_and_resets_on_failure(caplog: Any) -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

    async def sensitive() -> None:
        try:
            with sensitive_http_request():
                entered.set()
                await released.wait()
                for name in ("httpx", "httpcore.http11", "httpcore.connection"):
                    logging.getLogger(name).warning("private synthetic marker")
                raise ValueError("synthetic failure")
        except ValueError:
            logging.getLogger("httpx").warning("scope restored")

    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(sensitive())
        await entered.wait()
        logging.getLogger("httpx").warning("ordinary concurrent request")
        released.set()
        await task
    assert "private synthetic marker" not in caplog.text
    assert "ordinary concurrent request" in caplog.text
    assert "scope restored" in caplog.text


class _AuditResult:
    def scalar_one_or_none(self) -> None:
        return None


class _AuditConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> _AuditResult:
        self.calls.append((str(statement), params))
        if params and "hit_count" in params and params["hit_count"] == 0:
            raise RuntimeError("synthetic audit failure")
        return _AuditResult()


class _AuditContext:
    def __init__(self, connection: _AuditConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _AuditConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _AuditEngine:
    def __init__(self, connection: _AuditConnection) -> None:
        self.connection = connection

    def begin(self) -> _AuditContext:
        return _AuditContext(self.connection)


@pytest.mark.asyncio
async def test_sensitive_hit_audit_casts_integer_count_and_propagates_failure() -> None:
    connection = _AuditConnection()
    store = SqlPipelineStore()
    store._engine = lambda: _AuditEngine(connection)  # type: ignore[method-assign]

    await store.audit_sensitive_hit(7, 2)

    sql, params = connection.calls[0]
    assert "CAST(:hit_count AS integer)" in sql
    assert "jsonb_build_object" in sql
    assert params["hit_count"] == 2
    assert isinstance(params["hit_count"], int)
    assert params["actor"] == "app:7"
    assert "word" not in sql
    lowered = str(params).casefold()
    assert "phone" not in lowered
    assert "content" not in lowered

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await store.audit_sensitive_hit(7, 0)
