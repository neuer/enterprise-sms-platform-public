"""同一 dedup 窗口的并发告警必须在真实 PostgreSQL 上只留下一条。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from app.services.alert_repository import SqlAlertRepository, SqlAlertService

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.fixture
async def accept_runtime() -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{password}'"))
        if database:
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept'))
    accept_url = owner_url.set(username="sms_accept", password=password)
    try:
        yield owner, accept_url
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _repository(url: URL) -> SqlAlertRepository:
    return SqlAlertRepository(cast(Any, SimpleNamespace(database_url=url)))


def _service(url: URL) -> SqlAlertService:
    return SqlAlertService(
        cast(
            Any,
            SimpleNamespace(
                database_url=url,
                alert_smtp_allowed_host_set=frozenset(),
            ),
        )
    )


async def _advisory_waiters(owner: AsyncEngine) -> int:
    async with owner.connect() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_stat_activity
                        WHERE state='active'
                          AND wait_event_type='Lock'
                          AND wait_event='advisory'
                          AND query LIKE '%pg_advisory_xact_lock%'
                        """
                    )
                )
            ).scalar_one()
        )


async def _wait_for_waiters(owner: AsyncEngine, expected: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if await _advisory_waiters(owner) >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"没有观察到 {expected} 个咨询锁等待者")


async def _counts(owner: AsyncEngine, dedup_key: str) -> tuple[int, int, int]:
    async with owner.connect() as connection:
        alerts = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM alert_log WHERE dedup_key=:dedup_key"),
                    {"dedup_key": dedup_key},
                )
            ).scalar_one()
        )
        outbox = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event o
                        WHERE o.event_type='alert.delivery'
                          AND o.aggregate_id IN (
                            SELECT id::text FROM alert_log WHERE dedup_key=:dedup_key
                          )
                        """
                    ),
                    {"dedup_key": dedup_key},
                )
            ).scalar_one()
        )
        orphans = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event o
                        WHERE o.event_type='alert.delivery'
                          AND NOT EXISTS (
                            SELECT 1 FROM alert_log a WHERE a.id::text=o.aggregate_id
                          )
                        """
                    )
                )
            ).scalar_one()
        )
    return alerts, outbox, orphans


@pytest.mark.asyncio
async def test_concurrent_emits_leave_one_log_sink_alert(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    key = f"same-{uuid4().hex}"
    service = _service(accept_url)
    gate = create_async_engine(accept_url)

    async def emit() -> None:
        with correlation_scope():
            await service.emit(
                alert_type="balance_low",
                level="warn",
                title="余额低",
                detail={"balance": 1, "threshold": 2},
                dedup_key=key,
                dedup_hours=4,
            )

    connection = await gate.connect()
    transaction = await connection.begin()
    try:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:dedup_key, 0))"),
            {"dedup_key": key},
        )
        tasks = [asyncio.create_task(emit()) for _ in range(3)]
        await _wait_for_waiters(owner, 3)
        await transaction.rollback()
        await asyncio.gather(*tasks)
        alerts, outbox, orphans = await _counts(owner, key)
        assert alerts == 1
        assert outbox == 0
        assert orphans == 0
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await gate.dispose()
        async with owner.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM outbox_event WHERE event_type='alert.delivery'")
            )
            await cleanup.execute(
                text("DELETE FROM alert_log WHERE dedup_key=:dedup_key"), {"dedup_key": key}
            )


@pytest.mark.asyncio
async def test_distinct_keys_and_expired_window_alert_again(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    repository = _repository(accept_url)
    first_key = f"anomaly:7:verify:2026-10-22:{uuid4().hex[:8]}"
    next_key = f"anomaly:7:verify:2026-10-23:{uuid4().hex[:8]}"
    other_key = f"other-{uuid4().hex}"
    try:
        with correlation_scope():
            first = await repository.claim(
                alert_type="anomaly",
                level="crit",
                title="验证码发送量异常",
                detail={"recommendation": "建议：核查该应用调用来源"},
                channels="log-sink",
                dedup_key=first_key,
                dedup_hours=4,
            )
            other = await repository.claim(
                alert_type="anomaly",
                level="warn",
                title="发送量异常",
                detail={"category": "notice"},
                channels="log-sink",
                dedup_key=other_key,
                dedup_hours=4,
            )
            rolled = await repository.claim(
                alert_type="anomaly",
                level="crit",
                title="验证码发送量异常",
                detail={"recommendation": "建议：核查该应用调用来源"},
                channels="log-sink",
                dedup_key=next_key,
                dedup_hours=4,
            )
            duplicate = await repository.claim(
                alert_type="anomaly",
                level="crit",
                title="验证码发送量异常",
                detail={"recommendation": "建议：核查该应用调用来源"},
                channels="log-sink",
                dedup_key=first_key,
                dedup_hours=4,
            )
        assert first is not None and other is not None and rolled is not None
        assert len({first, other, rolled}) == 3
        assert duplicate is None
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE alert_log
                    SET created_at=now()-interval '5 hours'
                    WHERE id=:id
                    """
                ),
                {"id": first},
            )
        with correlation_scope():
            again = await repository.claim(
                alert_type="anomaly",
                level="crit",
                title="验证码发送量异常",
                detail={"recommendation": "建议：核查该应用调用来源"},
                channels="log-sink",
                dedup_key=first_key,
                dedup_hours=4,
            )
        assert again is not None and again != first
        alerts, outbox, orphans = await _counts(owner, first_key)
        assert alerts == 2
        assert outbox == 0
        assert orphans == 0
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM alert_log WHERE dedup_key=ANY(:keys)"),
                {"keys": [first_key, next_key, other_key]},
            )


@pytest.mark.asyncio
async def test_winner_rollback_lets_the_waiter_create_one_alert_and_outbox(
    accept_runtime: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, accept_url = accept_runtime
    repository = _repository(accept_url)
    key = f"rollback-{uuid4().hex}"
    started = asyncio.Event()
    release = asyncio.Event()
    from app.services import alert_repository as alert_repository_module

    real_enqueue = alert_repository_module.enqueue_outbox

    async def flaky(connection: object, spec: object, **kwargs: object) -> object:
        if not started.is_set():
            started.set()
            await release.wait()
            raise RuntimeError("synthetic outbox failure")
        return await real_enqueue(connection, spec, **kwargs)

    monkeypatch.setattr(alert_repository_module, "enqueue_outbox", flaky)

    async def claim() -> int | None:
        with correlation_scope():
            return await repository.claim(
                alert_type="balance_low",
                level="warn",
                title="余额低",
                detail={"balance": 1},
                channels="wecom",
                dedup_key=key,
                dedup_hours=4,
            )

    winner = asyncio.create_task(claim())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        waiter = asyncio.create_task(claim())
        await _wait_for_waiters(owner, 1)
        release.set()
        with pytest.raises(RuntimeError, match="synthetic outbox failure"):
            await winner
        alert_id = await waiter
        assert alert_id is not None
        alerts, outbox, orphans = await _counts(owner, key)
        assert alerts == 1
        assert outbox == 1
        assert orphans == 0
        async with owner.connect() as connection:
            aggregate_id = (
                await connection.execute(
                    text(
                        """
                        SELECT aggregate_id FROM outbox_event
                        WHERE event_type='alert.delivery' AND aggregate_id=:alert_id
                        """
                    ),
                    {"alert_id": str(alert_id)},
                )
            ).scalar_one()
        assert aggregate_id == str(alert_id)
    finally:
        release.set()
        if not winner.done():
            winner.cancel()
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE event_type='alert.delivery'
                      AND aggregate_id IN (
                        SELECT id::text FROM alert_log WHERE dedup_key=:dedup_key
                      )
                    """
                ),
                {"dedup_key": key},
            )
            await connection.execute(
                text("DELETE FROM alert_log WHERE dedup_key=:dedup_key"),
                {"dedup_key": key},
            )
