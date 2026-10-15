"""离线 SQL 必须在首条迁移操作前安装与在线一致的事务期限。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command

import app.settings
from tests.migration_environment import BASE_REVISION, NEXT_REVISION, migration_config


@pytest.mark.parametrize("start", ["base", BASE_REVISION])
def test_offline_transaction_has_deadlines_before_revision_sql(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    start: str,
) -> None:
    monkeypatch.setattr(
        app.settings,
        "get_settings",
        lambda: SimpleNamespace(database_owner_url="postgresql+asyncpg://owner@localhost/test"),
    )
    config = migration_config(tmp_path, ("SELECT 1",))
    command.upgrade(config, f"{start}:{NEXT_REVISION}", sql=True)
    assert isinstance(config.output_buffer, StringIO)
    sql = config.output_buffer.getvalue()
    assert sql.count("BEGIN;") == sql.count("COMMIT;") == 1
    assert sql.count("SET LOCAL lock_timeout='5s';") == 1
    assert sql.count("SET LOCAL statement_timeout='30s';") == 1
    assert (
        sql.index("BEGIN;")
        < sql.index("SET LOCAL lock_timeout='5s';")
        < sql.index("SET LOCAL statement_timeout='30s';")
        < sql.index("CREATE TABLE" if start == "base" else "INSERT INTO")
        < sql.index("COMMIT;")
    )
    assert (
        sql.index("SET LOCAL statement_timeout='30s';")
        < sql.index("-- Running upgrade")
        < sql.index("COMMIT;")
    )
