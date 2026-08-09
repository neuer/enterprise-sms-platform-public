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
            ).mappings().all()
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
    finally:
        await cleanup()
        await engine.dispose()
