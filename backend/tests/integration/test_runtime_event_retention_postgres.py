"""官方一次性 PostgreSQL 中验证观测归档守恒、并发、回滚和权限。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.services.housekeeping import LifecyclePolicy
from app.services.housekeeping_repository import SqlHousekeepingRepository
from app.services.metrics_repository import (
    SEND_DB_FAILURE_EVENT_TOTALS_SQL,
    WORKER_LEASE_EVENT_TOTALS_SQL,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires official isolated migrated PostgreSQL",
)
CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
STREAMS = {
    "worker_lease": (
        "worker_lease_event", "worker_lease_events", WORKER_LEASE_EVENT_TOTALS_SQL,
        "task_kind", "event_type", "callback", "acquired",
    ),
    "send_db_failure": (
        "send_db_failure_event", "send_db_failure_events", SEND_DB_FAILURE_EVENT_TOTALS_SQL,
        "phase", "reason", "claim", "db_timeout",
    ),
}
ROLES = (
    "sms_auth", "sms_accept", "sms_send", "sms_callback",
    "sms_export", "sms_scheduler", "sms_metrics",
)


@pytest.fixture
async def engines() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    owner = create_async_engine(os.environ["OUTBOX_POSTGRES_DSN"])
    runtime = create_async_engine(os.environ["OUTBOX_POSTGRES_DSN"])

    @event.listens_for(runtime.sync_engine, "connect")
    def runtime_role(connection: Any, _record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("SET ROLE sms_send")
        cursor.close()

    try:
        yield owner, runtime
    finally:
        await runtime.dispose()
        await owner.dispose()


async def totals(connection: AsyncConnection, stream: str) -> dict[tuple[str, str], int]:
    _, _, sql, first, second, _, _ = STREAMS[stream]
    result = await connection.execute(text(sql))
    return {(row[first], row[second]): int(row["count"]) for row in result.mappings()}


async def seed(owner: AsyncEngine, stream: str, *, old: int, recent: int = 0) -> list[Any]:
    table, _, _, first, second, first_value, second_value = STREAMS[stream]
    ids = []
    async with owner.begin() as connection:
        for index in range(old + recent):
            created = CUTOFF - timedelta(days=31 if index < old else 29)
            dimension = (
                first_value if index % 2 == 0
                else "export" if stream == "worker_lease" else "finalize"
            )
            if stream == "worker_lease":
                result = await connection.execute(text(f"""
                    INSERT INTO {table}({first},{second},task_id,created_at)
                    VALUES(:first,:second,1,:created) RETURNING id
                """), {"first": dimension, "second": second_value, "created": created})
            else:
                result = await connection.execute(text(f"""
                    INSERT INTO {table}(id,{first},{second},created_at)
                    VALUES(:id,:first,:second,:created) RETURNING id
                """), {
                    "id": uuid4(), "first": dimension, "second": second_value, "created": created,
                })
            ids.append(result.scalar_one())
    return ids


async def cleanup(owner: AsyncEngine, stream: str, ids: list[Any]) -> None:
    table = STREAMS[stream][0]
    async with owner.begin() as connection:
        await connection.execute(text(f"DELETE FROM {table} WHERE id=ANY(:ids)"), {"ids": ids})


def repository(runtime: AsyncEngine) -> SqlHousekeepingRepository:
    repo = SqlHousekeepingRepository(cast(Any, SimpleNamespace(database_url=runtime.url)))
    repo._engine = lambda: runtime  # type: ignore[method-assign]
    return repo


@pytest.mark.parametrize("stream", STREAMS)
async def test_compaction_is_bounded_preserves_recent_and_cumulative_metrics(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    owner, runtime = engines
    table, cleanup_kind, _, _, _, _, _ = STREAMS[stream]
    ids = await seed(owner, stream, old=7, recent=2)
    repo = repository(runtime)
    try:
        async with owner.connect() as connection:
            expected = await totals(connection, stream)
        amounts = []
        for _ in range(5):
            page = await repo.cleanup_page(
                cleanup_kind, LifecyclePolicy(90, 90, 30), cutoff=CUTOFF, cursor=None, limit=3,
            )
            amounts.append(page.affected)
            assert page.counts.runtime_events == page.affected
            async with owner.connect() as connection:
                assert await totals(connection, stream) == expected
        assert amounts == [3, 3, 1, 0, 0]
        async with owner.connect() as connection:
            remaining = (await connection.execute(
                text(f"SELECT id FROM {table} WHERE id=ANY(:ids)"), {"ids": ids}
            )).scalars().all()
            assert set(remaining) == set(ids[-2:])
            assert int((await connection.execute(
                text(f"SELECT count(*) FROM {table}_rollup")
            )).scalar_one()) <= (55 if stream == "worker_lease" else 10)
    finally:
        await cleanup(owner, stream, ids)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.concurrency
async def test_compaction_concurrency_new_events_and_locked_rows_preserve_totals(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    owner, runtime = engines
    table = STREAMS[stream][0]
    ids = await seed(owner, stream, old=17)
    try:
        async with owner.connect() as connection:
            before = await totals(connection, stream)
        async with owner.begin() as locked:
            await locked.execute(
                text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE"), {"id": ids[0]},
            )

            async def compact() -> int:
                async with runtime.begin() as connection:
                    return int((await connection.execute(text("""
                        SELECT compact_runtime_events(:stream,:cutoff,10)
                    """), {"stream": stream, "cutoff": CUTOFF})).scalar_one())

            results = await asyncio.gather(compact(), compact())
            assert sum(results) == 16
            new_ids = await seed(owner, stream, old=1)
            ids.extend(new_ids)
            async with owner.connect() as connection:
                after = await totals(connection, stream)
                key = (STREAMS[stream][5], STREAMS[stream][6])
                assert after[key] == before[key] + 1
        assert await compact() == 2
        assert await compact() == 0
        async with owner.connect() as connection:
            assert await totals(connection, stream) == after
    finally:
        await cleanup(owner, stream, ids)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.fault_injection
async def test_compaction_rollback_and_uncommitted_scrape_never_lose_or_double_count(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    owner, runtime = engines
    ids = await seed(owner, stream, old=4)
    try:
        async with owner.connect() as connection:
            before = await totals(connection, stream)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            async with runtime.begin() as compacting:
                assert (await compacting.execute(text("""
                    SELECT compact_runtime_events(:stream,:cutoff,4)
                """), {"stream": stream, "cutoff": CUTOFF})).scalar_one() == 4
                async with owner.connect() as connection:
                    assert await totals(connection, stream) == before
                raise RuntimeError("synthetic rollback after compaction")
        async with owner.connect() as connection:
            assert await totals(connection, stream) == before
        page = await repository(runtime).cleanup_page(
            STREAMS[stream][1], LifecyclePolicy(90, 90, 30),
            cutoff=CUTOFF, cursor=None, limit=4,
        )
        assert page.affected == 4
        async with owner.connect() as connection:
            assert await totals(connection, stream) == before
    finally:
        await cleanup(owner, stream, ids)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.fault_injection
async def test_accumulator_failure_rolls_back_deleted_rows(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    owner, _ = engines
    table, _, _, first, second, first_value, second_value = STREAMS[stream]
    ids = await seed(owner, stream, old=2)
    try:
        async with owner.connect() as connection:
            # 人工上界仅存在当前事务；累加溢出在 DELETE RETURNING 之后失败。
            await connection.execute(text(f"""
                INSERT INTO {table}_rollup({first},{second},event_count)
                VALUES(:first,:second,9223372036854775807)
                ON CONFLICT({first},{second}) DO UPDATE SET event_count=9223372036854775807
            """), {"first": first_value, "second": second_value})
            with pytest.raises(Exception, match="bigint out of range"):
                async with connection.begin_nested():
                    await connection.execute(text("""
                        SELECT compact_runtime_events(:stream,:cutoff,2)
                    """), {"stream": stream, "cutoff": CUTOFF})
            assert (await connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE id=ANY(:ids)"), {"ids": ids},
            )).scalar_one() == 2
            await connection.rollback()
    finally:
        await cleanup(owner, stream, ids)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.fault_injection
async def test_lock_timeout_preserves_committed_pages_and_retries_remaining(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    owner, runtime = engines
    table, cleanup_kind, *_ = STREAMS[stream]
    ids = await seed(owner, stream, old=5)
    repo = repository(runtime)

    async def page() -> int:
        result = await repo.cleanup_page(
            cleanup_kind, LifecyclePolicy(90, 90, 30), cutoff=CUTOFF, cursor=None, limit=2,
        )
        return result.affected

    try:
        async with owner.connect() as connection:
            before = await totals(connection, stream)
        assert await page() == 2
        async with owner.begin() as locked:
            await locked.execute(text(f"SELECT * FROM {table}_rollup FOR UPDATE"))
            with pytest.raises(Exception, match="lock timeout"):
                await page()
            async with owner.connect() as connection:
                assert (await connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE id=ANY(:ids)"), {"ids": ids},
                )).scalar_one() == 3
                assert await totals(connection, stream) == before
        assert await page() == 2
        assert await page() == 1
        assert await page() == 0
        async with owner.connect() as connection:
            assert await totals(connection, stream) == before
    finally:
        await cleanup(owner, stream, ids)


async def test_compaction_rejects_invalid_policy_and_requests(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    owner, runtime = engines
    for invalid in ("0", "-1", "366", "broken"):
        # 当前事务结束后配置自动回滚，其他测试不能继承无效策略。
        async with owner.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("""
                    UPDATE sys_config SET value=:value WHERE key='runtime_event_retention_days'
                """), {"value": invalid})
                with pytest.raises(Exception, match="invalid runtime event retention policy"):
                    await connection.execute(text("""
                        SELECT compact_runtime_events('worker_lease',:cutoff,1)
                    """), {"cutoff": CUTOFF})
            finally:
                await transaction.rollback()
    for valid in ("1", "365"):
        async with owner.connect() as connection:
            await connection.execute(text("""
                UPDATE sys_config SET value=:value WHERE key='runtime_event_retention_days'
            """), {"value": valid})
            assert (await connection.execute(text("""
                SELECT compact_runtime_events('worker_lease','1900-01-01+00',1)
            """))).scalar_one() == 0
            await connection.rollback()
    for stream, cutoff, limit in (
        ("other", CUTOFF, 1), ("worker_lease", CUTOFF, 0),
        ("worker_lease", CUTOFF, 1001),
        ("worker_lease", datetime.now(UTC) + timedelta(days=1), 1),
    ):
        async with runtime.connect() as connection:
            with pytest.raises(Exception, match="invalid runtime event compaction request"):
                await connection.execute(text("""
                    SELECT compact_runtime_events(:stream,:cutoff,:limit)
                """), {"stream": stream, "cutoff": cutoff, "limit": limit})


@pytest.mark.authorization
async def test_runtime_roles_cannot_bypass_compaction_and_metrics_can_read(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    owner, _ = engines
    for role in ROLES:
        for table in (
            "worker_lease_event", "send_db_failure_event",
            "worker_lease_event_rollup", "send_db_failure_event_rollup", "audit_log",
        ):
            for statement in (f"DELETE FROM {table} WHERE false", f"TRUNCATE {table}"):
                async with owner.connect() as connection:
                    await connection.execute(text(f"SET ROLE {role}"))
                    with pytest.raises(Exception, match="permission denied"):
                        await connection.execute(text(statement))
        async with owner.connect() as connection:
            await connection.execute(text(f"SET ROLE {role}"))
            if role == "sms_send":
                assert (await connection.execute(text("""
                    SELECT compact_runtime_events('worker_lease','1900-01-01+00',1)
                """))).scalar_one() == 0
            else:
                with pytest.raises(Exception, match="permission denied"):
                    await connection.execute(text("""
                        SELECT compact_runtime_events('worker_lease','1900-01-01+00',1)
                    """))
        for statement in (
            "UPDATE worker_lease_event SET event_type=event_type WHERE false",
            "UPDATE send_db_failure_event SET reason=reason WHERE false",
            "UPDATE audit_log SET actor=actor WHERE false",
            "UPDATE worker_lease_event_rollup SET event_count=event_count WHERE false",
            "UPDATE send_db_failure_event_rollup SET event_count=event_count WHERE false",
            "INSERT INTO worker_lease_event_rollup VALUES('raw','dead',0)",
            "INSERT INTO send_db_failure_event_rollup VALUES('claim','unexpected',0)",
        ):
            async with owner.connect() as connection:
                await connection.execute(text(f"SET ROLE {role}"))
                with pytest.raises(Exception, match="permission denied"):
                    await connection.execute(text(statement))
    async with owner.connect() as connection:
        await connection.execute(text("SET ROLE sms_metrics"))
        for stream in STREAMS:
            await totals(connection, stream)


async def test_downgrade_preserves_version_and_rejects_compacted_history(
    engines: tuple[AsyncEngine, AsyncEngine], monkeypatch: Any,
) -> None:
    owner, _ = engines
    path = Path(__file__).parents[2] / "migrations/versions/0128_runtime_event_retention.py"
    spec = importlib.util.spec_from_file_location("runtime_event_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
    module.downgrade()
    async with owner.connect() as connection:
        version = (await connection.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one()
    async with owner.connect() as connection:
        await connection.execute(text("""
            INSERT INTO worker_lease_event_rollup(task_kind,event_type,event_count)
            VALUES('raw','dead',1) ON CONFLICT(task_kind,event_type)
            DO UPDATE SET event_count=worker_lease_event_rollup.event_count+1
        """))
        with pytest.raises(Exception, match="runtime event rollup requires a compatible reader"):
            await connection.execute(text(statements[0]))
    async with owner.connect() as connection:
        assert (await connection.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one() == version
