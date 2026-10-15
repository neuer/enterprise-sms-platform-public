"""用临时 revision 验证生产 Alembic 入口，避免改动已发布迁移。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic.config import Config

BASE_REVISION = "0001_deadline_base"
NEXT_REVISION = "0002_deadline_change"


def migration_config(directory: Path, statements: tuple[str, ...]) -> Config:
    """复制当前生产入口，并建立只操作合成测试表的独立 revision 链。"""

    versions = directory / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "migrations/env.py"
    (directory / "env.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    baseline = (
        "CREATE TABLE deadline_probe(id integer PRIMARY KEY, note text NOT NULL)",
        "INSERT INTO deadline_probe VALUES (1, 'retained')",
        "CREATE TABLE deadline_target(id integer PRIMARY KEY)",
    )
    change = (
        "INSERT INTO deadline_probe VALUES (2, "
        "current_setting('lock_timeout') || '/' || current_setting('statement_timeout'))",
        "CREATE TABLE deadline_scratch(id integer)",
        *statements,
    )
    for revision, previous, sql in (
        (BASE_REVISION, None, baseline),
        (NEXT_REVISION, BASE_REVISION, change),
    ):
        calls = "\n".join(f"    op.execute({statement!r})" for statement in sql)
        (versions / f"{revision}.py").write_text(
            f"from alembic import op\nrevision = {revision!r}\n"
            f"down_revision = {previous!r}\nbranch_labels = None\ndepends_on = None\n"
            f"def upgrade():\n{calls}\n",
            encoding="utf-8",
        )
    config = Config(output_buffer=StringIO())
    config.set_main_option("script_location", str(directory))
    return config
