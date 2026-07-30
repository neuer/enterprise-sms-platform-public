"""以 schema.sql 建立 v1.6 数据库基线。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement

revision: str = "0001_schema_v16"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class RawSchemaDDL(ExecutableDDLElement):
    """绕过绑定参数和百分号替换，逐字承载规范 schema。"""

    inherit_cache = False

    def __init__(self, statement: str) -> None:
        self.statement = statement


@compiles(RawSchemaDDL)
def compile_raw_schema(element: RawSchemaDDL, _compiler: Any, **_kwargs: Any) -> str:
    """把 schema 原文直接交给数据库驱动。"""

    return element.statement


def _read_schema() -> str:
    """从容器或仓库根读取唯一的规范 schema 文件。"""

    revision_file = Path(__file__).resolve()
    candidates = (
        revision_file.parents[2] / "schema.sql",
        revision_file.parents[3] / "schema.sql",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise RuntimeError("canonical schema.sql not found")


def split_sql_statements(sql: str) -> tuple[str, ...]:
    """按顶层分号无损切分 PostgreSQL SQL，保留所有原始字符。"""

    boundaries: list[int] = []
    state = "normal"
    block_depth = 0
    dollar_tag = ""
    index = 0

    while index < len(sql):
        char = sql[index]
        following = sql[index : index + 2]

        if state == "normal":
            if following == "--":
                state = "line_comment"
                index += 2
                continue
            if following == "/*":
                state = "block_comment"
                block_depth = 1
                index += 2
                continue
            if char == "'":
                state = "single_quote"
            elif char == '"':
                state = "double_quote"
            elif char == "$":
                match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
                if match is not None:
                    dollar_tag = match.group(0)
                    state = "dollar_quote"
                    index += len(dollar_tag)
                    continue
            elif char == ";":
                boundaries.append(index + 1)
        elif state == "single_quote":
            if following == "''":
                index += 2
                continue
            if char == "'":
                state = "normal"
        elif state == "double_quote":
            if following == '""':
                index += 2
                continue
            if char == '"':
                state = "normal"
        elif state == "line_comment":
            if char in "\r\n":
                state = "normal"
        elif state == "block_comment":
            if following == "/*":
                block_depth += 1
                index += 2
                continue
            if following == "*/":
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "normal"
                continue
        elif state == "dollar_quote" and sql.startswith(dollar_tag, index):
            state = "normal"
            index += len(dollar_tag)
            continue

        index += 1

    if state not in {"normal", "line_comment"}:
        raise RuntimeError(f"unterminated SQL construct: {state}")
    if not boundaries:
        return (sql,) if sql.strip() else ()

    statements: list[str] = []
    start = 0
    for boundary in boundaries:
        statements.append(sql[start:boundary])
        start = boundary
    remainder = sql[start:]
    if remainder.strip():
        statements.append(remainder)
    elif remainder:
        statements[-1] += remainder
    return tuple(statements)


def upgrade() -> None:
    """逐字执行规范 schema，不维护第二份建表定义。"""

    for statement in split_sql_statements(_read_schema()):
        op.execute(RawSchemaDDL(statement))


def downgrade() -> None:
    """基线禁止自动执行破坏性回滚。"""

    raise RuntimeError("baseline downgrade is intentionally disabled")
