from __future__ import annotations

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
from app.services.admin import AuditQuery
from app.services.admin_repository import SqlAdminRepository
from app.services.approval import ApprovalService, SelfApprovalDenied
from app.services.approval_repository import SqlApprovalRepository
from app.services.export import ExportFilterSet
from app.services.export_repository import SqlExportRepository
from app.services.import_repository import ImportStateConflict, SqlImportRepository
from app.services.imports import ImportPhone, ImportResult
from app.services.vendor_test_operation import VendorTestOperationConflict
from app.services.vendor_test_operation_repository import (
    SqlVendorTestOperationRepository,
)

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

OLD_LOGIN = "stable-principal-old"
NEW_LOGIN = "stable-principal-new"
AD_LOGIN = "stable-principal-ad"
REUSER_LOGIN = OLD_LOGIN
OPERATION_ID = "c0a80101-0000-4000-8000-000000000191"
BATCH_NO = "c" * 32


class _UnusedPort:
    async def __getattr__(self, _name: str) -> None:
        raise AssertionError("本人审批必须在产生副作用前被拒绝")


def _filters() -> ExportFilterSet:
    return ExportFilterSet(None, None, None, None, None, None, (), "平台部")


@pytest.mark.asyncio
async def test_stable_principal_survives_rename_and_blocks_login_reuse(
    tmp_path: Any,
) -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
            import_storage_dir=tmp_path,
        ),
    )
    engine = create_async_engine(database_url)
    export_repository = SqlExportRepository(settings)
    import_repository = SqlImportRepository(settings)
    operation_repository = SqlVendorTestOperationRepository(settings)
    approval_repository = SqlApprovalRepository(settings)
    admin_repository = SqlAdminRepository(settings)
    account_ids: list[int] = []
    import_id: str | None = None
    export_public_id: str | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            stale_ids = list(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT DISTINCT account_id FROM auth_identity
                            WHERE normalized_login_name=ANY(CAST(:logins AS varchar(64)[]))
                            """
                        ),
                        {"logins": [OLD_LOGIN, NEW_LOGIN, AD_LOGIN]},
                    )
                ).scalars()
            )
            ids = sorted(set(account_ids + [int(value) for value in stale_ids]))
            await connection.execute(
                text(
                    """
                    DELETE FROM approval
                    WHERE batch_id IN (
                      SELECT id FROM sms_batch WHERE batch_no=:batch_no
                    )
                    """
                ),
                {"batch_no": BATCH_NO},
            )
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no=:batch_no"),
                {"batch_no": BATCH_NO},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM import_phone
                    WHERE import_task_id IN (
                      SELECT id FROM import_task
                      WHERE import_id=CAST(:import_id AS uuid)
                    )
                    """
                ),
                {"import_id": import_id},
            )
            await connection.execute(
                text("DELETE FROM import_task WHERE import_id=CAST(:import_id AS uuid)"),
                {"import_id": import_id},
            )
            await connection.execute(
                text("DELETE FROM export_task WHERE public_id=CAST(:public_id AS uuid)"),
                {"public_id": export_public_id},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_operation WHERE id=CAST(:id AS uuid)"),
                {"id": OPERATION_ID},
            )
            if ids:
                await connection.execute(
                    text(
                        """
                        DELETE FROM audit_log
                        WHERE actor_account_id=ANY(CAST(:ids AS bigint[]))
                        """
                    ),
                    {"ids": ids},
                )
                await connection.execute(
                    text("DELETE FROM auth_identity WHERE account_id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": ids},
                )
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": ids},
                )

    async def create_account(
        login: str,
        *,
        provider_code: str = "local",
        role: str = "operator",
        account_id: int | None = None,
    ) -> tuple[int, int]:
        async with engine.begin() as connection:
            if account_id is None:
                account_id = int(
                    (
                        await connection.execute(
                            text(
                                """
                                INSERT INTO user_account(display_name,dept,role)
                                VALUES(:login,'平台部',:role) RETURNING id
                                """
                            ),
                            {"login": login, "role": role},
                        )
                    ).scalar_one()
                )
                account_ids.append(account_id)
            provider_id = int(
                (
                    await connection.execute(
                        text("SELECT id FROM auth_provider WHERE code=:code"),
                        {"code": provider_code},
                    )
                ).scalar_one()
            )
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
                            "external_subject": f"{provider_code}:{login}",
                        },
                    )
                ).scalar_one()
            )
        return account_id, identity_id

    try:
        await cleanup()
        owner_account_id, local_identity_id = await create_account(OLD_LOGIN)
        _, ad_identity_id = await create_account(
            AD_LOGIN,
            provider_code="ad",
            account_id=owner_account_id,
        )
        old_principal = SecurityPrincipal(
            owner_account_id,
            local_identity_id,
            OLD_LOGIN,
            "平台部",
            "operator",
        )

        with audit_principal_scope(old_principal), correlation_scope(uuid4()):
            export_task = await export_repository.create(
                principal=old_principal,
                filters=_filters(),
                decrypted=False,
            )
            export_public_id = str(export_task.public_id)
            stored_import = await import_repository.persist(
                ImportResult(
                    [
                        ImportPhone(
                            b"ciphertext-only", "a" * 64, "138****8000", 1, 2
                        )
                    ],
                    [],
                ),
                principal=old_principal,
                filename="phones.csv",
                expire_hours=6,
            )
            import_id = stored_import.import_id
            await operation_repository.reserve_start(
                OPERATION_ID,
                "pause",
                principal=old_principal,
                conflicting_types=frozenset({"pause", "reset_configuration"}),
            )
        async with engine.begin() as connection:
            batch_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_batch(
                              batch_no,category,channel,creator,
                              creator_account_id,creator_identity_id,
                              dept,content,send_content_enc,status
                            ) VALUES(
                              :batch_no,'notice','web',:creator,
                              :account_id,:identity_id,
                              '平台部','维护通知',:ciphertext,'pending_approval'
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_no": BATCH_NO,
                            "creator": OLD_LOGIN,
                            "account_id": owner_account_id,
                            "identity_id": local_identity_id,
                            "ciphertext": b"ciphertext-only",
                        },
                    )
                ).scalar_one()
            )
            approval_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO approval(
                              batch_id,applicant,applicant_account_id,
                              applicant_identity_id,dept,expires_at
                            ) VALUES(
                              :batch_id,:applicant,:account_id,
                              :identity_id,'平台部',now()+interval '1 hour'
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "applicant": OLD_LOGIN,
                            "account_id": owner_account_id,
                            "identity_id": local_identity_id,
                        },
                    )
                ).scalar_one()
            )
            await connection.execute(
                text(
                    """
                    UPDATE auth_identity
                    SET login_name=:new_login,
                        normalized_login_name=:new_login,
                        external_subject=:external_subject
                    WHERE id=:identity_id
                    """
                ),
                {
                    "new_login": NEW_LOGIN,
                    "external_subject": f"local:{NEW_LOGIN}",
                    "identity_id": local_identity_id,
                },
            )

        renamed_principal = SecurityPrincipal(
            owner_account_id,
            local_identity_id,
            NEW_LOGIN,
            "平台部",
            "operator",
        )
        ad_principal = SecurityPrincipal(
            owner_account_id,
            ad_identity_id,
            AD_LOGIN,
            "平台部",
            "operator",
        )
        reused_account_id, reused_identity_id = await create_account(REUSER_LOGIN)
        reused_principal = SecurityPrincipal(
            reused_account_id,
            reused_identity_id,
            REUSER_LOGIN,
            "平台部",
            "operator",
        )

        assert (
            await export_repository.get_accessible(
                export_task.public_id,
                principal=renamed_principal,
                retention_days=7,
            )
            is not None
        )
        assert (
            await export_repository.get_accessible(
                export_task.public_id,
                principal=ad_principal,
                retention_days=7,
            )
            is not None
        )
        assert (
            await export_repository.get_accessible(
                export_task.public_id,
                principal=reused_principal,
                retention_days=7,
            )
            is None
        )
        with pytest.raises(ImportStateConflict):
            await import_repository.reserve(
                stored_import.import_id,
                principal=reused_principal,
            )
        import_reservation = await import_repository.reserve(
            stored_import.import_id,
            principal=ad_principal,
        )
        assert import_reservation.phones
        assert await import_repository.release(
            import_reservation.reservation_id,
            principal=renamed_principal,
        )

        with audit_principal_scope(renamed_principal), correlation_scope(uuid4()):
            replay = await operation_repository.reserve_start(
                OPERATION_ID,
                "pause",
                principal=renamed_principal,
                conflicting_types=frozenset({"pause", "reset_configuration"}),
            )
        assert replay.actor_account_id == owner_account_id
        with pytest.raises(VendorTestOperationConflict):
            await operation_repository.reserve_start(
                OPERATION_ID,
                "pause",
                principal=reused_principal,
                conflicting_types=frozenset({"pause", "reset_configuration"}),
            )

        with (
            audit_principal_scope(ad_principal),
            correlation_scope(uuid4()),
            pytest.raises(SelfApprovalDenied),
        ):
            await ApprovalService(
                approval_repository,
                cast(Any, _UnusedPort()),
                cast(Any, _UnusedPort()),
                cast(Any, _UnusedPort()),
            ).decide(
                approval_id,
                action="approve",
                principal=ad_principal,
                reason=None,
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE export_task
                    SET status='done',file_path='/synthetic/export.smsx',
                        row_count=1,finished_at=now()
                    WHERE public_id=:public_id
                    """
                ),
                {"public_id": export_public_id},
            )
        with audit_principal_scope(renamed_principal), correlation_scope(uuid4()):
            downloadable = await export_repository.get_downloadable_and_audit(
                export_task.public_id,
                principal=renamed_principal,
                ip="10.0.0.8",
                retention_days=7,
            )
        assert downloadable is not None
        audits, total = await admin_repository.list_audits(
            AuditQuery(
                None,
                None,
                None,
                None,
                None,
                1,
                100,
                actor_account_id=owner_account_id,
            )
        )
        assert total >= 4
        assert {item.actor for item in audits} >= {OLD_LOGIN, NEW_LOGIN}
        assert all(item.actor_account_id == owner_account_id for item in audits)

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT creator,creator_account_id,creator_identity_id
                        FROM sms_batch WHERE batch_no=:batch_no
                        """
                    ),
                    {"batch_no": BATCH_NO},
                )
            ).mappings().one()
        assert row == {
            "creator": OLD_LOGIN,
            "creator_account_id": owner_account_id,
            "creator_identity_id": local_identity_id,
        }
    finally:
        await cleanup()
        await engine.dispose()
