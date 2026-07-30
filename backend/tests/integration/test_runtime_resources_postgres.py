from __future__ import annotations

import asyncio
import os
from contextlib import suppress

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

import app.core.runtime_resources as resources

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_bounded_pool_recovers_dead_connections_and_closes_without_leak() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    resources._BUDGETS["background"] = resources.DatabasePoolBudget(
        pool_size=2,
        max_overflow=1,
        pool_timeout_seconds=0.25,
        connect_timeout_seconds=1,
        statement_timeout_ms=5_000,
    )
    engine = resources.database_engine(
        database_url,
        component="background",
    )
    active = 0
    peak = 0

    async def load() -> None:
        nonlocal active, peak
        async with engine.connect() as connection:
            active += 1
            peak = max(peak, active)
            try:
                await connection.execute(text("SELECT pg_sleep(0.03)"))
            finally:
                active -= 1

    await asyncio.gather(*(load() for _ in range(12)))
    assert peak <= 3

    admin = create_async_engine(database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE application_name='sms-background'
                      AND pid<>pg_backend_pid()
                    """
                )
            )
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await admin.dispose()

    snapshot = next(
        item
        for item in resources.resource_snapshot().database_components
        if item.component == "background"
    )
    assert snapshot.open <= snapshot.budget == 3
    assert snapshot.checked_out == 0
    assert snapshot.timeouts == 0

    await resources.close_runtime_resources()
    closed = next(
        item
        for item in resources.resource_snapshot().database_components
        if item.component == "background"
    )
    assert closed.open == 0
    assert closed.checked_out == 0
    assert closed.leaked_on_shutdown == 0


@pytest.mark.asyncio
async def test_connect_failure_is_bounded_and_same_pool_recovers() -> None:
    target = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    target_host = target.host or "127.0.0.1"
    target_port = target.port or 5432
    proxy_tasks: set[asyncio.Task[None]] = set()

    async def proxy(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        server_reader, server_writer = await asyncio.open_connection(
            target_host,
            target_port,
        )
        await asyncio.gather(
            _pipe(client_reader, server_writer),
            _pipe(server_reader, client_writer),
        )

    def track_proxy(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(proxy(reader, writer))
        proxy_tasks.add(task)
        task.add_done_callback(proxy_tasks.discard)

    reservation = await asyncio.start_server(track_proxy, "127.0.0.1", 0)
    proxy_port = int(reservation.sockets[0].getsockname()[1])
    reservation.close()
    await reservation.wait_closed()

    proxied_url = target.set(host="127.0.0.1", port=proxy_port)
    resources._BUDGETS["background"] = resources.DatabasePoolBudget(
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=0.25,
        connect_timeout_seconds=0.25,
        statement_timeout_ms=5_000,
    )
    engine = resources.database_engine(proxied_url, component="background")

    async def probe() -> int:
        async with engine.connect() as connection:
            return int(await connection.scalar(text("SELECT 1")))

    with pytest.raises((OSError, ConnectionError, TimeoutError, DBAPIError)):
        await asyncio.wait_for(probe(), timeout=1)

    server = await asyncio.start_server(
        track_proxy,
        "127.0.0.1",
        proxy_port,
    )
    try:
        assert await asyncio.wait_for(probe(), timeout=2) == 1
    finally:
        await resources.close_runtime_resources()
        server.close()
        await server.wait_closed()
        if proxy_tasks:
            await asyncio.wait_for(
                asyncio.gather(*proxy_tasks, return_exceptions=True),
                timeout=2,
            )
