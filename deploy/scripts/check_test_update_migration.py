#!/usr/bin/env python3
"""快速更新 Alembic 路径的 expand-only 静态检查器。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


class ExpandOnlyError(ValueError):
    """迁移路径包含破坏性或无法静态确认的操作。"""


@dataclass(frozen=True, slots=True)
class CheckedMigration:
    revision: str
    down_revision: str | None
    path: Path


_SAFE_OP_METHODS = frozenset(
    {
        "add_column",
        "create_check_constraint",
        "create_foreign_key",
        "create_index",
        "create_primary_key",
        "create_table",
        "create_unique_constraint",
    }
)
_DESTRUCTIVE_SQL = re.compile(
    r"\bDROP\s+TABLE\b"
    r"|\bALTER\s+TABLE\b.*\bDROP\s+COLUMN\b"
    r"|\bTRUNCATE(?:\s+TABLE)?\b"
    r"|\bDELETE\s+FROM\b"
    r"|\bALTER\s+TABLE\b.*\bALTER\s+COLUMN\b.*\bTYPE\b"
    r"|\bALTER\s+TABLE\b.*\bRENAME\s+(?:COLUMN|TO)\b",
    re.IGNORECASE | re.DOTALL,
)
_DROP_TRIGGER_RE = re.compile(
    r"\bDROP\s+TRIGGER\s+IF\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_CREATE_TRIGGER_RE = re.compile(
    r"\bCREATE\s+TRIGGER\s+([A-Za-z_][A-Za-z0-9_]*)\b.*\bON\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)
_DROP_CONSTRAINT_RE = re.compile(
    r"\bALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_ADD_CONSTRAINT_RE = re.compile(
    r"\bALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"ADD\s+CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _literal_assignment(tree: ast.Module, name: str, path: Path) -> str | None:
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and (
            isinstance(value.value, str) or value.value is None
        ):
            return value.value
        raise ExpandOnlyError(f"{path.name}: {name} is not a static literal")
    raise ExpandOnlyError(f"{path.name}: missing {name}")


def _load_migrations(directory: Path) -> dict[str, tuple[CheckedMigration, ast.Module]]:
    migrations: dict[str, tuple[CheckedMigration, ast.Module]] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ExpandOnlyError(f"cannot parse migration: {path.name}") from exc
        revision = _literal_assignment(tree, "revision", path)
        down_revision = _literal_assignment(tree, "down_revision", path)
        if revision is None:
            raise ExpandOnlyError(f"{path.name}: revision cannot be null")
        if revision in migrations:
            raise ExpandOnlyError(f"duplicate migration revision: {revision}")
        migrations[revision] = (
            CheckedMigration(revision, down_revision, path),
            tree,
        )
    return migrations


def find_migration_head(directory: Path) -> str:
    """返回唯一迁移头；分叉或空目录一律 fail closed。"""

    migrations = _load_migrations(directory)
    parents = {
        migration.down_revision
        for migration, _tree in migrations.values()
        if migration.down_revision is not None
    }
    heads = sorted(set(migrations) - parents)
    if len(heads) != 1:
        raise ExpandOnlyError("migration graph must have exactly one head")
    return heads[0]


def _upgrade_function(tree: ast.Module, migration: CheckedMigration) -> ast.FunctionDef:
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    ]
    if len(functions) != 1:
        raise ExpandOnlyError(f"{migration.revision}: upgrade function is invalid")
    return functions[0]


def _op_method(call: ast.Call) -> str | None:
    function = call.func
    if not isinstance(function, ast.Attribute) or not isinstance(function.value, ast.Name):
        return None
    if function.value.id != "op":
        return None
    return function.attr


def _literal_sql(call: ast.Call, migration: CheckedMigration) -> str:
    if len(call.args) != 1 or call.keywords:
        raise ExpandOnlyError(
            f"{migration.revision}: cannot statically confirm raw SQL"
        )
    argument = call.args[0]
    if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
        raise ExpandOnlyError(
            f"{migration.revision}: cannot statically confirm raw SQL"
        )
    return argument.value.strip()


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """只解析模块级字符串常量，不执行迁移代码。"""

    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def _resolved_split_loop_sql(
    upgrade: ast.FunctionDef,
    tree: ast.Module,
) -> dict[int, tuple[str, ...]]:
    """解析 ``for x in MODULE_SQL.split(';'): op.execute(x)`` 的静态 SQL。"""

    constants = _module_string_constants(tree)
    resolved: dict[int, tuple[str, ...]] = {}
    for loop in (node for node in ast.walk(upgrade) if isinstance(node, ast.For)):
        if not isinstance(loop.target, ast.Name) or not isinstance(loop.iter, ast.Call):
            continue
        splitter = loop.iter.func
        if (
            not isinstance(splitter, ast.Attribute)
            or splitter.attr != "split"
            or not isinstance(splitter.value, ast.Name)
            or splitter.value.id not in constants
            or len(loop.iter.args) != 1
            or not isinstance(loop.iter.args[0], ast.Constant)
            or loop.iter.args[0].value != ";"
            or loop.iter.keywords
        ):
            continue
        statements = tuple(
            statement.strip()
            for statement in constants[splitter.value.id].split(";")
            if statement.strip()
        )
        for call in (node for node in ast.walk(loop) if isinstance(node, ast.Call)):
            if (
                _op_method(call) == "execute"
                and len(call.args) == 1
                and not call.keywords
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == loop.target.id
            ):
                resolved[id(call)] = statements
    return resolved


def _check_raw_sql(
    call: ast.Call,
    migration: CheckedMigration,
    *,
    paired_triggers: frozenset[tuple[str, str]] = frozenset(),
    paired_constraints: frozenset[tuple[str, str]] = frozenset(),
    resolved_sql: str | None = None,
) -> None:
    sql = resolved_sql if resolved_sql is not None else _literal_sql(call, migration)
    normalized = re.sub(r"\s+", " ", sql).upper()
    if normalized.startswith(("GRANT ", "REVOKE ")):
        if ";" in sql.rstrip().removesuffix(";"):
            raise ExpandOnlyError(
                f"{migration.revision}: cannot statically confirm raw SQL"
            )
        return
    if normalized.startswith("ALTER DEFAULT PRIVILEGES "):
        if " REVOKE " not in f" {normalized} " or ";" in sql.rstrip().removesuffix(";"):
            raise ExpandOnlyError(
                f"{migration.revision}: cannot statically confirm raw SQL"
            )
        return
    if re.fullmatch(r"ALTER ROLE [A-Z_][A-Z0-9_]* NOLOGIN", normalized):
        return
    if _DESTRUCTIVE_SQL.search(sql):
        if re.search(r"\bDROP\s+TABLE\b", sql, re.IGNORECASE):
            detail = "DROP TABLE"
        elif re.search(r"\bTRUNCATE(?:\s+TABLE)?\b", sql, re.IGNORECASE):
            detail = "TRUNCATE TABLE"
        elif re.search(r"\bDELETE\s+FROM\b", sql, re.IGNORECASE):
            detail = "DELETE FROM"
        else:
            detail = "destructive ALTER TABLE"
        raise ExpandOnlyError(f"{migration.revision}: destructive SQL: {detail}")
    if normalized.startswith(("UPDATE ", "INSERT INTO ")):
        if ";" in sql.rstrip().removesuffix(";"):
            raise ExpandOnlyError(
                f"{migration.revision}: cannot statically confirm raw SQL"
            )
        return
    if normalized.startswith(
        (
            "CREATE TABLE ",
            "CREATE INDEX ",
            "CREATE UNIQUE INDEX ",
            "CREATE SEQUENCE ",
            "CREATE OR REPLACE FUNCTION ",
            "CREATE TRIGGER ",
        )
    ):
        return
    dropped_trigger = _DROP_TRIGGER_RE.fullmatch(sql.strip())
    if dropped_trigger is not None:
        key = (dropped_trigger.group(2).lower(), dropped_trigger.group(1).lower())
        if key in paired_triggers:
            return
        raise ExpandOnlyError(
            f"{migration.revision}: trigger drop is not replaced in the same migration"
        )
    if normalized.startswith("ALTER TABLE "):
        dropped_constraint = _DROP_CONSTRAINT_RE.fullmatch(sql.strip())
        if dropped_constraint is not None:
            key = (
                dropped_constraint.group(1).lower(),
                dropped_constraint.group(2).lower(),
            )
            if key in paired_constraints:
                return
            raise ExpandOnlyError(
                f"{migration.revision}: constraint drop is not replaced "
                "in the same migration"
            )
        if (
            " ADD COLUMN " in f" {normalized} "
            or " ADD CONSTRAINT " in f" {normalized} "
            or " VALIDATE CONSTRAINT " in f" {normalized} "
            or re.search(
                r"\bALTER\s+COLUMN\s+[A-Z_][A-Z0-9_]*\s+SET\s+NOT\s+NULL\b",
                normalized,
            )
            or re.search(
                r"\bALTER\s+COLUMN\s+[A-Z_][A-Z0-9_]*\s+SET\s+DEFAULT\b",
                normalized,
            )
        ):
            return
    if normalized.startswith("DO "):
        if "ALTER TABLE " not in normalized:
            raise ExpandOnlyError(
                f"{migration.revision}: cannot statically confirm raw SQL"
            )
        if re.search(
            r"\bDROP\b|\bRENAME\b|\bTRUNCATE\b|\bDELETE\s+FROM\b",
            normalized,
        ):
            raise ExpandOnlyError(
                f"{migration.revision}: destructive SQL: destructive ALTER TABLE"
            )
        if (
            " ADD COLUMN " in f" {normalized} "
            or " ADD CONSTRAINT " in f" {normalized} "
            or " VALIDATE CONSTRAINT " in f" {normalized} "
        ):
            return
    raise ExpandOnlyError(f"{migration.revision}: cannot statically confirm raw SQL")


def _check_upgrade(migration: CheckedMigration, tree: ast.Module) -> None:
    upgrade = _upgrade_function(tree, migration)
    resolved_split_sql = _resolved_split_loop_sql(upgrade, tree)
    raw_sql_calls = [
        node
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call) and _op_method(node) == "execute"
    ]
    literal_sql = [
        sql
        for call in raw_sql_calls
        for sql in (
            resolved_split_sql.get(id(call))
            or (_literal_sql(call, migration),)
        )
    ]
    dropped_triggers = {
        (match.group(2).lower(), match.group(1).lower())
        for sql in literal_sql
        if (match := _DROP_TRIGGER_RE.fullmatch(sql.strip())) is not None
    }
    created_triggers = {
        (match.group(2).lower(), match.group(1).lower())
        for sql in literal_sql
        for match in _CREATE_TRIGGER_RE.finditer(sql)
    }
    dropped_constraints = {
        (match.group(1).lower(), match.group(2).lower())
        for sql in literal_sql
        if (match := _DROP_CONSTRAINT_RE.fullmatch(sql.strip())) is not None
    }
    added_constraints = {
        (match.group(1).lower(), match.group(2).lower())
        for sql in literal_sql
        for match in _ADD_CONSTRAINT_RE.finditer(sql)
    }
    paired_triggers = frozenset(dropped_triggers & created_triggers)
    paired_constraints = frozenset(dropped_constraints & added_constraints)
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        method = _op_method(node)
        if method is None:
            continue
        if method == "execute":
            statements = resolved_split_sql.get(id(node))
            if statements is None:
                statements = (_literal_sql(node, migration),)
            for statement in statements:
                _check_raw_sql(
                    node,
                    migration,
                    paired_triggers=paired_triggers,
                    paired_constraints=paired_constraints,
                    resolved_sql=statement,
                )
            continue
        if method not in _SAFE_OP_METHODS:
            raise ExpandOnlyError(
                f"{migration.revision}: Alembic operation is not expand-only: op.{method}"
            )


def check_expand_only(
    directory: Path,
    current_revision: str,
    target_revision: str,
) -> tuple[CheckedMigration, ...]:
    """检查 current 到 target 的唯一直线升级路径，不允许反向或 destructive DDL。"""

    migrations = _load_migrations(directory)
    if current_revision == target_revision:
        return ()
    if current_revision not in migrations or target_revision not in migrations:
        raise ExpandOnlyError("migration revision is unknown")
    reversed_path: list[tuple[CheckedMigration, ast.Module]] = []
    cursor = target_revision
    visited: set[str] = set()
    while cursor != current_revision:
        if cursor in visited:
            raise ExpandOnlyError("migration graph contains a cycle")
        visited.add(cursor)
        item = migrations.get(cursor)
        if item is None:
            raise ExpandOnlyError("migration path is incomplete")
        reversed_path.append(item)
        parent = item[0].down_revision
        if parent is None:
            raise ExpandOnlyError("downgrade or unrelated migration path is forbidden")
        cursor = parent
    path = list(reversed(reversed_path))
    for migration, tree in path:
        _check_upgrade(migration, tree)
    return tuple(migration for migration, _tree in path)
