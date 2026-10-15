"""官方一次性 PostgreSQL 中验证观测归档守恒、并发、回滚和权限。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

import app.settings
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
RETENTION_REVISION = "0128_runtime_event_retention"
BACKEND = Path(__file__).resolve().parents[2]


def migration_config(directory: Path, *, legacy_revision: str | None = None) -> Config:
    """原样复制生产迁移；旧基线仅扣除本轮新增对象，不替换 env 或 revision 实现。"""
    source = BACKEND / "migrations"
    shutil.copytree(source, directory / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    schema = (BACKEND.parent / "schema.sql").read_text(encoding="utf-8")
    if legacy_revision is not None:
        assert legacy_revision in {"0126_admission_recovery_mode", "0127_callback_app_admission"}
        spec = importlib.util.spec_from_file_location(
            "retention_schema_parser", source / "versions/0001_schema_v16.py",
        )
        assert spec and spec.loader
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        # 系统参数的大 INSERT 只移除一个新 key，其他种子逐字保留。
        schema, changed = re.subn(
            r"^\('runtime_event_retention_days'.*\n", "", schema, flags=re.MULTILINE,
        )
        assert changed == 1
        names: tuple[str, ...] = (
            "worker_lease_event_rollup", "send_db_failure_event_rollup",
            "idx_worker_lease_event_retention", "idx_send_db_failure_event_retention",
            "compact_runtime_events",
        )
        if legacy_revision.startswith("0126_"):
            names += ("idx_cb_app_admission",)
        statements = baseline.split_sql_statements(schema)
        removed = tuple(statement for statement in statements if any(n in statement for n in names))
        # 0128 只新增两表、两索引、一函数和五条权限语句；0127 只新增一索引。
        assert len(removed) == (11 if legacy_revision.startswith("0126_") else 10)
        assert all(re.search(r"\b(?:CREATE|GRANT|REVOKE)\b", item) for item in removed)
        schema = "".join(statement for statement in statements if statement not in removed)
    (directory / "schema.sql").write_text(schema, encoding="utf-8")
    config = Config()
    config.set_main_option("script_location", str(directory / "migrations"))
    return config


@pytest.fixture
async def migration_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, URL]]:
    """只在官方临时集群内创建本测试独占库，结束时不用 FORCE 隐藏连接泄漏。"""
    owner_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    baseline = os.environ.get("SMS_ISOLATED_TEST_DATABASE")
    if (
        not baseline or owner_url.host != "127.0.0.1"
        or (
            owner_url.database != baseline
            and re.fullmatch(r"gate_[0-9a-f]{32}", owner_url.database or "") is None
        )
    ):
        raise RuntimeError("retention migration tests require the official isolated database")
    database = f"retention_{uuid4().hex}"
    admin = create_async_engine(owner_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
    url = owner_url.set(database=database)
    owner = create_async_engine(url)
    key_files = []
    for name in ("data_aes_key", "data_hmac_key"):
        path = tmp_path / name
        path.write_bytes(base64.b64encode(secrets.token_bytes(32)))
        path.chmod(0o600)
        key_files.append(path)
    settings = app.settings.Settings(
        environment="test", debug=True, auth_mock=True, vendor_mock=True,
        db_host="127.0.0.1", db_port=owner_url.port or 5432, db_name=database,
        db_owner_password_file=Path(os.environ["DB_OWNER_PASSWORD_FILE"]),
        data_aes_key_file=key_files[0], data_hmac_key_file=key_files[1],
    )
    monkeypatch.setattr(
        app.settings, "get_settings", lambda: settings,
    )
    try:
        yield owner, url
    finally:
        await owner.dispose()
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{database}"'))
        finally:
            await admin.dispose()


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
async def test_concurrent_compactors_skip_locked_rows_and_resume(
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
            async with owner.connect() as connection:
                assert await totals(connection, stream) == before
        assert await compact() == 1
        assert await compact() == 0
        async with owner.connect() as connection:
            assert await totals(connection, stream) == before
    finally:
        await cleanup(owner, stream, ids)


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.concurrency
async def test_insert_commits_during_blocked_compaction_without_snapshot_count_drift(
    engines: tuple[AsyncEngine, AsyncEngine], stream: str,
) -> None:
    """真实行锁证明压缩已经执行并等待，插入在解除屏障之前提交。"""
    owner, runtime = engines
    table, _, _, first, second, first_value, second_value = STREAMS[stream]
    ids = await seed(owner, stream, old=2)
    async with owner.begin() as connection:
        await connection.execute(text(f"""
            INSERT INTO {table}_rollup({first},{second},event_count)
            VALUES(:first,:second,0) ON CONFLICT({first},{second}) DO NOTHING
        """), {"first": first_value, "second": second_value})
    started = asyncio.Event()
    compacting_pid: int | None = None

    async def compact() -> int:
        nonlocal compacting_pid
        async with runtime.begin() as connection:
            compacting_pid = int((await connection.execute(
                text("SELECT pg_backend_pid()")
            )).scalar_one())
            # 测试屏障有自身上限；生产 repository 的 1s/5s 另有真实超时验收。
            await connection.execute(text("SET LOCAL lock_timeout='5s'"))
            await connection.execute(text("SET LOCAL statement_timeout='10s'"))
            started.set()
            return int((await connection.execute(text("""
                SELECT compact_runtime_events(:stream,:cutoff,10)
            """), {"stream": stream, "cutoff": CUTOFF})).scalar_one())

    compact_task: asyncio.Task[int] | None = None
    try:
        async with owner.connect() as snapshot:
            snapshot = await snapshot.execution_options(isolation_level="REPEATABLE READ")
            before = await totals(snapshot, stream)
            async with owner.begin() as blocker:
                blocker_pid = (await blocker.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                await blocker.execute(text(f"""
                    SELECT event_count FROM {table}_rollup
                    WHERE {first}=:first AND {second}=:second FOR UPDATE
                """), {"first": first_value, "second": second_value})
                compact_task = asyncio.create_task(compact())
                await asyncio.wait_for(started.wait(), timeout=3)

                async def wait_for_database_barrier() -> None:
                    async with owner.connect() as observer:
                        while True:
                            if compact_task is not None and compact_task.done():
                                await compact_task
                                raise AssertionError("compaction finished before database barrier")
                            blocked = await observer.execute(text("""
                                SELECT :blocker=ANY(pg_blocking_pids(:compacting))
                            """), {"blocker": blocker_pid, "compacting": compacting_pid})
                            if blocked.scalar_one():
                                return
                            await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_database_barrier(), timeout=3)
                new_ids = await seed(owner, stream, old=1, recent=1)
                ids.extend(new_ids)
                assert not compact_task.done()
                assert await totals(snapshot, stream) == before
                async with owner.connect() as connection:
                    after_insert = await totals(connection, stream)
                primary = (first_value, second_value)
                secondary = ("export" if stream == "worker_lease" else "finalize", second_value)
                expected = dict(before)
                expected[primary] = expected.get(primary, 0) + 1
                expected[secondary] = expected.get(secondary, 0) + 1
                assert after_insert == expected
            # 原 SQL 快照看不到后插入行，本页只能压缩屏障前的两条。
            assert await asyncio.wait_for(compact_task, timeout=5) == 2
            assert await totals(snapshot, stream) == before
            await snapshot.rollback()
        async with owner.connect() as connection:
            assert await totals(connection, stream) == expected
            assert set((await connection.execute(
                text(f"SELECT id FROM {table} WHERE id=ANY(:ids)"), {"ids": ids},
            )).scalars()) == set(new_ids)
        assert await compact() == 1
        assert await compact() == 0
        async with owner.connect() as connection:
            assert await totals(connection, stream) == expected
    finally:
        if compact_task is not None and not compact_task.done():
            compact_task.cancel()
            await asyncio.gather(compact_task, return_exceptions=True)
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


async def persisted_events(connection: AsyncConnection, stream: str) -> list[tuple[Any, ...]]:
    table = STREAMS[stream][0]
    result = await connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))
    return [tuple(row) for row in result]


@pytest.mark.parametrize("legacy_revision", (
    "0126_admission_recovery_mode", "0127_callback_app_admission",
))
async def test_real_upgrade_preserves_events_from_each_legacy_revision(
    migration_database: tuple[AsyncEngine, URL], tmp_path: Path, legacy_revision: str,
) -> None:
    owner, _ = migration_database
    config = migration_config(tmp_path, legacy_revision=legacy_revision)
    await asyncio.to_thread(command.upgrade, config, legacy_revision)
    async with owner.connect() as connection:
        assert (await connection.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one() == legacy_revision
        for relation in (
            "worker_lease_event_rollup", "send_db_failure_event_rollup",
            "idx_worker_lease_event_retention", "idx_send_db_failure_event_retention",
        ):
            assert (await connection.execute(
                text("SELECT to_regclass(:name)"), {"name": relation},
            )).scalar_one() is None
        assert (await connection.execute(text("""
            SELECT to_regprocedure('compact_runtime_events(text,timestamptz,integer)')
        """))).scalar_one() is None
        assert (await connection.execute(text("""
            SELECT count(*) FROM sys_config WHERE key='runtime_event_retention_days'
        """))).scalar_one() == 0
        callback_index = (await connection.execute(
            text("SELECT to_regclass('idx_cb_app_admission')")
        )).scalar_one()
        assert (callback_index is not None) == legacy_revision.startswith("0127_")
    for stream in STREAMS:
        await seed(owner, stream, old=3, recent=2)
    async with owner.connect() as connection:
        before = {stream: await persisted_events(connection, stream) for stream in STREAMS}
    await asyncio.to_thread(command.upgrade, config, RETENTION_REVISION)
    async with owner.connect() as connection:
        assert (await connection.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one() == RETENTION_REVISION
        for stream, rows in before.items():
            assert await persisted_events(connection, stream) == rows
            assert sum((await totals(connection, stream)).values()) == len(rows) == 5
            table = STREAMS[stream][0]
            assert (await connection.execute(
                text(f"SELECT count(*) FROM {table}_rollup")
            )).scalar_one() == 0
        assert (await connection.execute(
            text("SELECT to_regclass('idx_cb_app_admission')")
        )).scalar_one() is not None
    for stream in STREAMS:
        async with owner.begin() as connection:
            assert (await connection.execute(text("""
                SELECT compact_runtime_events(:stream,:cutoff,10)
            """), {"stream": stream, "cutoff": CUTOFF})).scalar_one() == 3
    async with owner.connect() as connection:
        for stream in STREAMS:
            assert sum((await totals(connection, stream)).values()) == 5
            rows = await persisted_events(connection, stream)
            assert len(rows) == 2
            assert all(row in before[stream] for row in rows)


async def test_real_alembic_downgrade_rejects_rollups_and_preserves_version_and_all_events(
    migration_database: tuple[AsyncEngine, URL], tmp_path: Path,
) -> None:
    owner, _ = migration_database
    config = migration_config(tmp_path)
    await asyncio.to_thread(command.upgrade, config, RETENTION_REVISION)
    for stream in STREAMS:
        await seed(owner, stream, old=3, recent=2)
        async with owner.begin() as connection:
            assert (await connection.execute(text("""
                SELECT compact_runtime_events(:stream,:cutoff,10)
            """), {"stream": stream, "cutoff": CUTOFF})).scalar_one() == 3

    async def snapshot() -> dict[str, Any]:
        async with owner.connect() as connection:
            result: dict[str, Any] = {
                "version": (await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )).scalar_one(),
            }
            for stream, (table, *_) in STREAMS.items():
                result[stream] = await persisted_events(connection, stream)
                result[f"{stream}_rollup"] = [tuple(row) for row in await connection.execute(
                    text(f"SELECT * FROM {table}_rollup ORDER BY 1,2")
                )]
                result[f"{stream}_totals"] = await totals(connection, stream)
            return result

    before = await snapshot()
    assert before["version"] == RETENTION_REVISION
    for stream in STREAMS:
        assert len(before[stream]) == 2
        assert sum(row[-1] for row in before[f"{stream}_rollup"]) == 3
    with pytest.raises(DBAPIError, match="runtime event rollup requires a compatible reader"):
        # 真实命令加载生产 env/0128；不是直接执行截取的 downgrade SQL。
        await asyncio.to_thread(command.downgrade, config, "0127_callback_app_admission")
    assert await snapshot() == before
