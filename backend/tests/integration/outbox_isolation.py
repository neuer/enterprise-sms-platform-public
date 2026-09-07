"""仅为全局领取语义的 Outbox 集成用例隔离一张表，保留真实审计与角色边界。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ROLES = (
    "sms_auth",
    "sms_accept",
    "sms_send",
    "sms_callback",
    "sms_export",
    "sms_scheduler",
    "sms_metrics",
)
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class ScopedOutboxEngine:
    """只改变本仓储事务的查表范围，继续使用原 managed engine 的审计 hook。"""

    def __init__(self, managed_engine: Any, schema: str) -> None:
        self._managed_engine = managed_engine
        self.schema = schema
        self._search_path = f"pg_catalog,{_identifier(schema)},public"

    async def _scope(self, connection: Any) -> None:
        await connection.execute(
            text("SELECT set_config('search_path',:search_path,TRUE)"),
            {"search_path": self._search_path},
        )

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        async with self._managed_engine.begin() as connection:
            await self._scope(connection)
            yield connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        # Outbox 的 connect 路径只读；首次 execute 开始事务，关闭时回滚局部设置。
        async with self._managed_engine.connect() as connection:
            await self._scope(connection)
            yield connection


async def _privileges(connection: Any, table: str) -> tuple[list[Any], list[Any]]:
    """读取七个真实角色的有效表/列 ACL，包括授权能力，不扩大 metrics 列权限。"""

    params = {"roles": list(ROLES), "table": table}
    table_rows = list(
        (
            await connection.execute(
                text(
                    """
        SELECT r.role_name,p.privilege,
          has_table_privilege(CAST(r.role_name AS name),CAST(:table AS text),p.privilege) allowed,
          has_table_privilege(CAST(r.role_name AS name),CAST(:table AS text),
            p.privilege||' WITH GRANT OPTION') grantable
        FROM unnest(CAST(:roles AS text[])) r(role_name)
        CROSS JOIN unnest(CAST(:privileges AS text[])) p(privilege)
        ORDER BY r.role_name,p.privilege
        """
                ),
                {**params, "privileges": list(TABLE_PRIVILEGES)},
            )
        ).mappings()
    )
    column_rows = list(
        (
            await connection.execute(
                text(
                    """
        SELECT r.role_name,a.attname,p.privilege,
          has_column_privilege(CAST(r.role_name AS name),CAST(:table AS regclass),
            a.attnum,p.privilege) allowed,
          has_column_privilege(CAST(r.role_name AS name),CAST(:table AS regclass),a.attnum,
            p.privilege||' WITH GRANT OPTION') grantable
        FROM pg_attribute a
        CROSS JOIN unnest(CAST(:roles AS text[])) r(role_name)
        CROSS JOIN unnest(CAST(:privileges AS text[])) p(privilege)
        WHERE a.attrelid=CAST(:table AS regclass) AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY r.role_name,a.attname,p.privilege
        """
                ),
                {**params, "privileges": list(COLUMN_PRIVILEGES)},
            )
        ).mappings()
    )
    return table_rows, column_rows


@asynccontextmanager
async def isolated_outbox(
    database_url: Any,
    managed_engine: Any,
) -> AsyncIterator[tuple[AsyncEngine, ScopedOutboxEngine]]:
    """返回直接 SQL 的私有 engine 和仓储 facade；调用方先清理自己的 public 审计/账号。"""

    assert os.environ.get("ENVIRONMENT") == "test"
    url = make_url(database_url)
    assert url.host in {"127.0.0.1", "localhost"}, "requires loopback PostgreSQL"
    schema = f"outbox_case_{uuid4().hex}"
    quoted_schema = _identifier(schema)
    table = f"{schema}.outbox_event"
    qualified_table = f"{quoted_schema}.outbox_event"
    engine = create_async_engine(
        url,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": f"pg_catalog,{quoted_schema},public"}},
    )
    created = False
    try:
        async with engine.begin() as connection:
            source = (
                (
                    await connection.execute(
                        text(
                            """
                SELECT CAST(c.relkind AS text) AS relkind,c.relrowsecurity,c.relforcerowsecurity,
                  c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) same_owner,
                  EXISTS(SELECT 1 FROM pg_constraint
                    WHERE conrelid=c.oid AND contype='f') has_foreign_key,
                  EXISTS(SELECT 1 FROM pg_trigger
                    WHERE tgrelid=c.oid AND NOT tgisinternal) has_trigger
                FROM pg_class c WHERE c.oid='public.outbox_event'::regclass
                """
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert source["relkind"] == "r" and source["same_owner"]
            assert not any(
                source[key]
                for key in (
                    "relrowsecurity",
                    "relforcerowsecurity",
                    "has_foreign_key",
                    "has_trigger",
                )
            ), "Outbox 新增表依赖时必须显式审查隔离策略，不能由 LIKE 悄悄省略"
            expected_tables, expected_columns = await _privileges(connection, "public.outbox_event")
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            created = True
            await connection.execute(
                text(f"CREATE TABLE {qualified_table} (LIKE public.outbox_event INCLUDING ALL)")
            )
            roles = ",".join(_identifier(role) for role in ROLES)
            await connection.execute(text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {roles}"))
            await connection.execute(
                text(f"REVOKE ALL ON TABLE {qualified_table} FROM PUBLIC,{roles}")
            )
            table_rights = {
                (row["role_name"], row["privilege"]): (row["allowed"], row["grantable"])
                for row in expected_tables
            }
            for row in expected_tables:
                if row["allowed"]:
                    option = " WITH GRANT OPTION" if row["grantable"] else ""
                    await connection.execute(
                        text(
                            f"GRANT {row['privilege']} ON TABLE {qualified_table} "
                            f"TO {_identifier(row['role_name'])}{option}"
                        )
                    )
            for row in expected_columns:
                table_allowed, table_grantable = table_rights[(row["role_name"], row["privilege"])]
                if not row["allowed"] or (
                    table_allowed and (not row["grantable"] or table_grantable)
                ):
                    continue
                option = " WITH GRANT OPTION" if row["grantable"] else ""
                await connection.execute(
                    text(
                        f"GRANT {row['privilege']} ({_identifier(row['attname'])}) "
                        f"ON TABLE {qualified_table} TO {_identifier(row['role_name'])}{option}"
                    )
                )
            actual_tables, actual_columns = await _privileges(connection, table)
            assert actual_tables == expected_tables, "隔离表的有效表 ACL 与 public 不一致"
            assert actual_columns == expected_columns, "隔离表的有效列 ACL 与 public 不一致"
            assert await connection.scalar(
                text(
                    "SELECT bool_and(has_schema_privilege("
                    "CAST(role_name AS name),:schema,'USAGE')) "
                    "FROM unnest(CAST(:roles AS text[])) r(role_name)"
                ),
                {"schema": schema, "roles": list(ROLES)},
            )
        yield engine, ScopedOutboxEngine(managed_engine, schema)
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text("SET LOCAL lock_timeout='5s'"))
                    await connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        finally:
            # managed engine 仍由现有测试的 close_runtime_resources 负责，不关闭他人资源。
            await engine.dispose()
