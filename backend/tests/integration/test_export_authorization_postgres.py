from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.export import ExportFilterSet
from app.services.export_repository import SqlExportRepository

pytestmark = pytest.mark.skipif(
    "EXPORT_AUTH_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def filters(scope_dept: str | None = "平台部") -> ExportFilterSet:
    return ExportFilterSet(None, None, None, None, None, None, (), scope_dept)


@pytest.mark.asyncio
async def test_real_postgres_export_scope_matrix_and_download_audit_are_fail_closed() -> None:
    database_url = make_url(os.environ["EXPORT_AUTH_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    repository = SqlExportRepository(settings)
    engine = create_async_engine(database_url)
    logins = (
        "export-pg-creator",
        "export-pg-same-dept",
        "export-pg-other-dept",
        "export-pg-admin",
    )
    account_ids: list[int] = []
    identity_ids: list[int] = []
    public_ids: list[str] = []

    async def cleanup() -> None:
        if public_ids:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        DELETE FROM audit_log
                        WHERE object_type='export_task'
                          AND object_id=ANY(CAST(:public_ids AS text[]))
                        """
                    ),
                    {"public_ids": public_ids},
                )
                await connection.execute(
                    text(
                        """
                        DELETE FROM export_task
                        WHERE public_id=ANY(CAST(:public_ids AS uuid[]))
                        """
                    ),
                    {"public_ids": public_ids},
                )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM auth_identity
                    WHERE normalized_login_name=ANY(CAST(:logins AS varchar(64)[]))
                    """
                ),
                {"logins": list(logins)},
            )
            if account_ids:
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": account_ids},
                )

    try:
        await cleanup()
        async with engine.begin() as connection:
            provider_id = int(
                (
                    await connection.execute(
                        text("SELECT id FROM auth_provider WHERE code='local'")
                    )
                ).scalar_one()
            )
            for login, dept, role in (
                (logins[0], "平台部", "approver"),
                (logins[1], "平台部", "approver"),
                (logins[2], "财务部", "approver"),
                (logins[3], "管理部", "admin"),
            ):
                account_id = int(
                    (
                        await connection.execute(
                            text(
                                """
                                INSERT INTO user_account(display_name,dept,role)
                                VALUES(:display_name,:dept,:role)
                                RETURNING id
                                """
                            ),
                            {"display_name": login, "dept": dept, "role": role},
                        )
                    ).scalar_one()
                )
                account_ids.append(account_id)
                identity_id = int(
                    (
                        await connection.execute(
                            text(
                                """
                                INSERT INTO auth_identity(
                                  account_id,provider_id,login_name,
                                  normalized_login_name,external_subject
                                ) VALUES(
                                  :account_id,:provider_id,:login,:login,:external_subject
                                ) RETURNING id
                                """
                            ),
                            {
                                "account_id": account_id,
                                "provider_id": provider_id,
                                "login": login,
                                "external_subject": f"local:{login}",
                            },
                        )
                    ).scalar_one()
                )
                identity_ids.append(identity_id)

        principals = [
            SecurityPrincipal(account_id, identity_id, login, dept, role)  # type: ignore[arg-type]
            for account_id, identity_id, (login, dept, role) in zip(
                account_ids,
                identity_ids,
                (
                    (logins[0], "平台部", "approver"),
                    (logins[1], "平台部", "approver"),
                    (logins[2], "财务部", "approver"),
                    (logins[3], "管理部", "admin"),
                ),
                strict=True,
            )
        ]
        with audit_principal_scope(principals[0]), correlation_scope(uuid4()):
            task = await repository.create(
                principal=principals[0],
                filters=filters(),
                decrypted=True,
            )
        public_ids.append(str(task.public_id))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE export_task
                    SET status='done',file_path='/synthetic/export.smsx',
                        row_count=2,finished_at=now()
                    WHERE public_id=:public_id
                    """
                ),
                {"public_id": str(task.public_id)},
            )
            unresolved = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO export_task(creator,filters,decrypted)
                        VALUES('unresolved-history','{}'::jsonb,FALSE)
                        RETURNING public_id
                        """
                    )
                )
            ).scalar_one()
            public_ids.append(str(unresolved))

        creator, same_dept, other_dept, admin, unresolved_admin = await asyncio.gather(
            repository.get_accessible(
                task.public_id,
                principal=principals[0],
                retention_days=7,
            ),
            repository.get_accessible(
                task.public_id,
                principal=principals[1],
                retention_days=7,
            ),
            repository.get_accessible(
                task.public_id,
                principal=principals[2],
                retention_days=7,
            ),
            repository.get_accessible(
                task.public_id,
                principal=principals[3],
                retention_days=7,
            ),
            repository.get_accessible(
                unresolved,
                principal=principals[3],
                retention_days=7,
            ),
        )

        assert creator is not None
        assert same_dept is not None
        assert other_dept is None
        assert admin is not None
        assert unresolved_admin is None

        with audit_principal_scope(principals[1]), correlation_scope(uuid4()):
            allowed_download = await repository.get_downloadable_and_audit(
                task.public_id,
                principal=principals[1],
                ip="10.0.0.8",
                retention_days=7,
            )
        with audit_principal_scope(principals[2]), correlation_scope(uuid4()):
            denied_download = await repository.get_downloadable_and_audit(
                task.public_id,
                principal=principals[2],
                ip="10.0.0.9",
                retention_days=7,
            )
        assert allowed_download is not None
        assert denied_download is None

        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT actor,role,ip::text,after_val
                        FROM audit_log
                        WHERE action='export_download' AND object_id=:public_id
                        """
                        ),
                        {"public_id": str(task.public_id)},
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 1
        assert rows[0]["actor"] == logins[1]
        assert rows[0]["role"] == "approver"
        assert rows[0]["ip"] == "10.0.0.8/32"
        assert rows[0]["after_val"] == {
            "actor_account_id": account_ids[1],
            "actor_identity_id": identity_ids[1],
            "scope_dept": "平台部",
            "decrypted": True,
            "row_count": 2,
        }
        # 降权/调岗后重新签发并验证 JWT，历史文件仍按新主体部门授权。
        from app.core.auth.jwt import JwtService
        from app.core.auth.users import SqlUserRepository
        from tests.integration.test_security_session_postgres import MemoryStore, claims

        users = SqlUserRepository(settings)
        tokens = JwtService(
            "synthetic-export-session-secret-long-enough",
            MemoryStore(),
            security_session_loader=users.load_security_session,
        )
        masked = []
        for scope in (None, "平台部", "财务部"):
            with audit_principal_scope(principals[0]), correlation_scope(uuid4()):
                historical = await repository.create(
                    principal=principals[0],
                    filters=filters(scope),
                    decrypted=False,
                )
            masked.append(historical)
            public_ids.append(str(historical.public_id))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE export_task SET status='done',file_path='/synthetic/export.smsx',"
                    "row_count=2,finished_at=now() WHERE public_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(item.public_id) for item in masked]},
            )
        for role in ("operator", "viewer"):
            for department in ("平台部", "财务部"):
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE user_account SET role=:role,dept=:dept WHERE id=:id"),
                        {"role": role, "dept": department, "id": account_ids[0]},
                    )
                projection = await users.load_security_session(account_ids[0], identity_ids[0])
                current = (await tokens.verify(tokens.issue(claims(projection)))).principal
                assert current.role == role and current.dept == department
                for historical, scope in zip(masked, (None, "平台部", "财务部"), strict=True):
                    accessible = await repository.get_accessible(
                        historical.public_id,
                        principal=current,
                        retention_days=7,
                    )
                    with audit_principal_scope(current), correlation_scope(uuid4()):
                        downloaded = await repository.get_downloadable_and_audit(
                            historical.public_id,
                            principal=current,
                            ip="127.0.0.1",
                            retention_days=7,
                        )
                    assert (accessible is not None) == (scope == department)
                    assert (downloaded is not None) == (scope == department)
                assert (
                    await repository.get_accessible(
                        task.public_id,
                        principal=current,
                        retention_days=7,
                    )
                    is None
                )

        # Historical admin-owned unmatched files cannot inherit approver ownership access.
        from dataclasses import replace

        for dataset in ("message", "unmatched", "future", None):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE export_task SET filters=CAST(:filters AS jsonb) WHERE public_id=:id"
                    ),
                    {
                        "filters": __import__("json").dumps({"dataset": dataset}),
                        "id": task.public_id,
                    },
                )
            for role in ("admin", "approver"):
                current = replace(principals[0], role=role)
                allowed = dataset == "message" or (dataset == "unmatched" and role == "admin")
                assert (
                    await repository.get_accessible(
                        task.public_id, principal=current, retention_days=7
                    )
                    is not None
                ) == allowed
                with audit_principal_scope(current), correlation_scope(uuid4()):
                    assert (
                        await repository.get_downloadable_and_audit(
                            task.public_id, principal=current, ip="127.0.0.1", retention_days=7
                        )
                        is not None
                    ) == allowed
    finally:
        await cleanup()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset", ["message", "unmatched"])
async def test_export_half_open_day_includes_fractional_last_second(dataset):
    from datetime import datetime

    from app.services.export_repository import _message_where, _params, _unmatched_where

    engine = create_async_engine(make_url(os.environ["EXPORT_AUTH_POSTGRES_DSN"]))
    end = datetime.fromisoformat("2026-02-01T00:00:00+08:00")
    value = ExportFilterSet(None, None, None, None, None, None, (), None, dataset, end)
    alias = "m" if dataset == "message" else "u"
    predicate = _message_where() if dataset == "message" else _unmatched_where()
    query = f"""SELECT {alias}.created_at FROM (
      SELECT stamp AS created_at, ''::text AS phone_hmac, ''::text AS status
      FROM (VALUES ('2026-01-31 23:59:59.999999+08'::timestamptz),
                   ('2026-02-01 00:00:00+08'::timestamptz)) moments(stamp)
    ) {alias} CROSS JOIN (SELECT ''::text AS dept, ''::text AS category,
                            1::bigint AS app_id, ''::text AS batch_no) b
    WHERE {predicate}"""
    try:
        async with engine.connect() as connection:
            rows = list(await connection.execute(text(query), _params(value)))
            assert len(rows) == 1 and rows[0][0].microsecond == 999999
            from dataclasses import replace

            inclusive = replace(value, end=end, end_exclusive=None)
            assert len(list(await connection.execute(text(query), _params(inclusive)))) == 2
    finally:
        await engine.dispose()
