"""真实 PostgreSQL 中验证生产迁移入口的有限等待、回滚与后续恢复。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

import app.settings
from tests.migration_environment import BASE_REVISION, NEXT_REVISION, migration_config

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated PostgreSQL 16",
)


async def connect(url: URL) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )


@pytest.fixture
async def migration_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[URL]:
    """仅官方一次性容器入口可创建临时空库；正常关闭后删除以检出连接泄漏。"""

    owner = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    if not os.environ.get("SMS_ISOLATED_TEST_DATABASE") or owner.host != "127.0.0.1":
        raise RuntimeError("migration deadline tests require the isolated database entrypoint")
    database = f"deadline_{uuid4().hex}"
    admin = await connect(owner.set(database="postgres"))
    await admin.execute(f'CREATE DATABASE "{database}" TEMPLATE template0')
    url = owner.set(database=database)
    monkeypatch.setattr(
        app.settings,
        "get_settings",
        lambda: SimpleNamespace(database_owner_url=url.render_as_string(hide_password=False)),
    )
    try:
        yield url
    finally:
        try:
            # 不用 FORCE：连接没有及时释放就让测试失败，不掩盖引擎清理缺陷。
            await admin.execute(f'DROP DATABASE "{database}"')
        finally:
            await admin.close()


async def assert_rolled_back(url: URL) -> None:
    connection = await connect(url)
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == BASE_REVISION
        assert [tuple(row) for row in await connection.fetch("SELECT * FROM deadline_probe")] == [
            (1, "retained")
        ]
        assert await connection.fetchval("SELECT to_regclass('deadline_scratch')") is None
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='deadline_target' AND column_name='changed')"
        )
    finally:
        await connection.close()


async def assert_committed(url: URL) -> None:
    connection = await connect(url)
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == NEXT_REVISION
        assert await connection.fetchval("SELECT note FROM deadline_probe WHERE id=2") == "5s/30s"
        assert await connection.fetchval("SELECT to_regclass('deadline_scratch')") is not None
    finally:
        await connection.close()


@pytest.mark.fault_injection
async def test_lock_timeout_rolls_back_whole_migration_then_recovers(
    migration_database: URL,
    tmp_path: Path,
) -> None:
    config = migration_config(tmp_path, ("ALTER TABLE deadline_target ADD COLUMN changed boolean",))
    await asyncio.to_thread(command.upgrade, config, BASE_REVISION)
    blocker = await connect(migration_database)
    transaction = blocker.transaction()
    await transaction.start()
    try:
        await blocker.execute("LOCK TABLE deadline_target IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(DBAPIError, match="lock timeout"):
            await asyncio.wait_for(asyncio.to_thread(command.upgrade, config, "head"), timeout=15)
        assert 4 <= time.monotonic() - started < 15
    finally:
        await transaction.rollback()
        await blocker.close()
    await assert_rolled_back(migration_database)
    await asyncio.to_thread(command.upgrade, config, "head")
    await assert_committed(migration_database)


@pytest.mark.fault_injection
async def test_statement_timeout_rolls_back_whole_migration_then_recovers(
    migration_database: URL,
    tmp_path: Path,
) -> None:
    config = migration_config(tmp_path, ("SELECT pg_sleep(35)",))
    await asyncio.to_thread(command.upgrade, config, BASE_REVISION)
    started = time.monotonic()
    with pytest.raises(DBAPIError, match="statement timeout"):
        await asyncio.wait_for(asyncio.to_thread(command.upgrade, config, "head"), timeout=45)
    assert 29 <= time.monotonic() - started < 45
    await assert_rolled_back(migration_database)
    # 保留相同 revision 身份和生产 env，只缩短合成的耗时工作以验证重试。
    config = migration_config(tmp_path, ("SELECT pg_sleep(0)",))
    await asyncio.to_thread(command.upgrade, config, "head")
    await assert_committed(migration_database)


@pytest.mark.parametrize("legacy_payload", ['{"batch_no":null}', '{"batch_no":true}'])
async def test_published_audit_constraint_preserves_history_on_success_or_failure(
    migration_database: URL,
    tmp_path: Path,
    legacy_payload: str,
) -> None:
    """在合成旧表上执行原版 0121；合法缺省保留，非法历史失败且原约束恢复。"""

    config = migration_config(tmp_path, ())
    path = tmp_path / "versions" / f"{NEXT_REVISION}.py"
    path.write_text(
        "from importlib import import_module\n"
        + path.read_text(encoding="utf-8")
        + "    import_module('migrations.versions.0121_audit_payload_shape').upgrade()\n",
        encoding="utf-8",
    )
    await asyncio.to_thread(command.upgrade, config, BASE_REVISION)
    connection = await connect(migration_database)
    try:
        await connection.execute(
            "CREATE TABLE audit_log(id integer PRIMARY KEY, created_at timestamptz NOT NULL "
            "DEFAULT now(), before_val jsonb, after_val jsonb, "
            "CONSTRAINT ck_audit_payload_shape CHECK "
            "(before_val IS NULL OR jsonb_typeof(before_val)='object'))"
        )
        await connection.execute(
            "INSERT INTO audit_log(id,after_val) VALUES(1,$1::jsonb)", legacy_payload
        )
        original = await connection.fetchrow("SELECT * FROM audit_log")
        original_check = await connection.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_audit_payload_shape'"
        )
        if legacy_payload == '{"batch_no":null}':
            await asyncio.to_thread(command.upgrade, config, "head")
            await assert_committed(migration_database)
            with pytest.raises(asyncpg.CheckViolationError, match="invalid audit batch reference"):
                await connection.execute(
                    "INSERT INTO audit_log(id,after_val) VALUES(2,'{\"batch_no\":null}'::jsonb)"
                )
        else:
            with pytest.raises(DBAPIError, match="ck_audit_payload_shape"):
                await asyncio.to_thread(command.upgrade, config, "head")
            await assert_rolled_back(migration_database)
            assert await connection.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_audit_payload_shape'"
            ) == original_check
        assert await connection.fetch("SELECT * FROM audit_log") == [original]
    finally:
        await connection.close()
