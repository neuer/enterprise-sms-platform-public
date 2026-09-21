"""官方一次性数据库内演练 0108 到 0119，禁止连接共享或生产数据库。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.skipif(
    not os.environ.get("SMS_ISOLATED_TEST_DATABASE"),
    reason="requires isolated migrated PostgreSQL",
)


def test_0108_upgrade_preserves_existing_config_and_reaches_0119() -> None:
    """使用独立空库建立旧迁移头，验证升级及重复 upgrade 不破坏已有配置。"""
    owner = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    assert owner.host == "127.0.0.1"
    assert owner.database and owner.database.startswith("gate_")
    database = "rebaseline_" + uuid4().hex
    backend = Path(__file__).resolve().parents[2]

    async def sql(database_name: str, statement: str) -> list[asyncpg.Record]:
        connection = await asyncpg.connect(
            host=owner.host, port=owner.port, user=owner.username,
            password=owner.password, database=database_name,
        )
        try:
            return await connection.fetch(statement)
        finally:
            await connection.close()

    def upgrade(revision: str) -> None:
        with tempfile.TemporaryDirectory(prefix="rebaseline-secret-") as directory:
            secret = Path(directory) / "db_owner_password"
            secret.touch(mode=0o600)
            secret.write_text(owner.password or "", encoding="utf-8")
            environment = dict(
                os.environ, DB_NAME=database, DB_HOST="127.0.0.1",
                DB_PORT=str(owner.port), DB_OWNER_PASSWORD_FILE=str(secret),
            )
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", revision],
                cwd=backend, env=environment, capture_output=True, text=True,
            )
        # 不回显子进程原始输出，避免异常 DSN 进入测试报告。
        returncode = result.returncode
        del result
        assert returncode == 0, f"isolated upgrade failed: {revision}"

    asyncio.run(sql("postgres", f'CREATE DATABASE "{database}" TEMPLATE template0'))
    try:
        # 使用服务器基线的不可变规范 schema，不用最新 schema 冒充旧版本。
        baseline = subprocess.run(
            ["git", "show", "d715624d18e6897f6916fcef9fbdc8622d41eb15:schema.sql"],
            cwd=backend, capture_output=True, text=True, check=True,
        ).stdout

        async def initialize() -> None:
            connection = await asyncpg.connect(
                host=owner.host, port=owner.port, user=owner.username,
                password=owner.password, database=database,
            )
            try:
                async with connection.transaction():
                    await connection.execute("""
                        CREATE TABLE alembic_version(version_num VARCHAR(64) PRIMARY KEY);
                        INSERT INTO alembic_version VALUES('0108_chunk_failover_pending');
                    """)
                    await connection.execute(baseline)
            finally:
                await connection.close()

        asyncio.run(initialize())
        assert asyncio.run(sql(database, "SELECT version_num FROM alembic_version"))[0][0] == (
            "0108_chunk_failover_pending"
        )
        asyncio.run(sql(database, """
            INSERT INTO sys_config(key,value,value_type,description)
            VALUES('rebaseline_synthetic_sentinel','preserve-me','str','synthetic rehearsal')
        """))
        for _ in range(2):
            upgrade("0119_temporary_password_expiry")
            assert asyncio.run(sql(database, "SELECT version_num FROM alembic_version"))[0][0] == (
                "0119_temporary_password_expiry"
            )
            assert asyncio.run(sql(database, """
                SELECT value FROM sys_config WHERE key='rebaseline_synthetic_sentinel'
            """))[0][0] == "preserve-me"
    finally:
        asyncio.run(sql("postgres", f'DROP DATABASE "{database}" WITH (FORCE)'))
