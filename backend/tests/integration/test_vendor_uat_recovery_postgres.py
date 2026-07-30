from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.services.vendor_test_operation import vendor_test_uat_biz_id
from app.services.vendor_test_operation_repository import (
    SqlVendorTestOperationRepository,
)
from app.services.vendor_test_uat import VendorTestUatReconciler

pytestmark = pytest.mark.skipif(
    "VENDOR_UAT_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

EXPIRED_ID = "c0a80101-0000-4000-8000-000000000181"
BATCH_ID = "c0a80101-0000-4000-8000-000000000182"
CONTROL_ID = "c0a80101-0000-4000-8000-000000000183"
CONTROL_RACE_ID = "c0a80101-0000-4000-8000-000000000184"
BATCH_NO = "b" * 32
APP_ID = 981


async def _create_principal(engine: Any, login: str) -> SecurityPrincipal:
    async with engine.begin() as connection:
        stale_accounts = (
            await connection.execute(
                text(
                    """
                    SELECT account_id FROM auth_identity
                    WHERE normalized_login_name=:login
                    """
                ),
                {"login": login},
            )
        ).scalars().all()
        await connection.execute(
            text("DELETE FROM auth_identity WHERE normalized_login_name=:login"),
            {"login": login},
        )
        if stale_accounts:
            await connection.execute(
                text("DELETE FROM user_account WHERE id=ANY(CAST(:ids AS bigint[]))"),
                {"ids": list(stale_accounts)},
            )
        provider_id = int(
            (
                await connection.execute(
                    text("SELECT id FROM auth_provider WHERE code='local'")
                )
            ).scalar_one()
        )
        account_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO user_account(display_name,dept,role)
                        VALUES(:login,'平台部','admin') RETURNING id
                        """
                    ),
                    {"login": login},
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
                        "external_subject": f"local:{login}",
                    },
                )
            ).scalar_one()
        )
    return SecurityPrincipal(account_id, identity_id, login, "平台部", "admin")


async def _delete_principal(engine: Any, principal: SecurityPrincipal) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM auth_identity WHERE account_id=:account_id"),
            {"account_id": principal.account_id},
        )
        await connection.execute(
            text("DELETE FROM user_account WHERE id=:account_id"),
            {"account_id": principal.account_id},
        )


@pytest.mark.asyncio
async def test_real_postgres_guard_expiry_and_batch_truth_recovery() -> None:
    database_url = make_url(os.environ["VENDOR_UAT_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    repository = SqlVendorTestOperationRepository(settings)
    engine = create_async_engine(database_url)
    principal: SecurityPrincipal | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE object_type='vendor_test_operation'
                      AND object_id IN (:expired_id,:batch_id)
                    """
                ),
                {"expired_id": EXPIRED_ID, "batch_id": BATCH_ID},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_operation WHERE id IN (:expired_id,:batch_id)"),
                {"expired_id": EXPIRED_ID, "batch_id": BATCH_ID},
            )
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no=:batch_no"),
                {"batch_no": BATCH_NO},
            )
            await connection.execute(
                text("DELETE FROM app WHERE id=:app_id"),
                {"app_id": APP_ID},
            )

    try:
        await cleanup()
        principal = await _create_principal(engine, "vendor-uat-pg-recovery")
        requested = await repository.reserve_start(
            EXPIRED_ID,
            "uat_send",
            principal=principal,
            conflicting_types=frozenset({"uat_send", "reset_configuration"}),
        )
        assert requested.status == "requested"
        running = await repository.claim_uat_running(EXPIRED_ID)
        assert running is not None and running.status == "running"
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE vendor_test_operation
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:id
                    """
                ),
                {"id": EXPIRED_ID},
            )
        async with repository.acceptance_guard(EXPIRED_ID):
            assert await repository.prepare_uat_acceptance(EXPIRED_ID) is False
            assert (
                await repository.expire_uat_if_stale(
                    EXPIRED_ID,
                    safe_code="UAT_ACCEPTANCE_EXPIRED",
                )
                is None
            )
        expired = await repository.expire_uat_if_stale(
            EXPIRED_ID,
            safe_code="UAT_ACCEPTANCE_EXPIRED",
        )
        assert expired is not None
        assert expired.status == "failed"
        assert expired.safe_code == "UAT_ACCEPTANCE_EXPIRED"

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO app(
                      id,name,dept,api_key_hash,api_key_prefix,created_by
                    ) VALUES(
                      :app_id,'UAT recovery test','平台部',
                      repeat('a',64),'uat98100','test'
                    )
                    """
                ),
                {"app_id": APP_ID},
            )
        await repository.reserve_start(
            BATCH_ID,
            "uat_send",
            principal=principal,
            conflicting_types=frozenset({"uat_send", "reset_configuration"}),
        )
        batch_running = await repository.claim_uat_running(BATCH_ID)
        assert batch_running is not None
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE vendor_test_operation
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:id
                    """
                ),
                {"id": BATCH_ID},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_batch(
                      batch_no,channel,app_id,dept,content,send_content_enc,biz_id,
                      is_test,status
                    ) VALUES(
                      :batch_no,'web',:app_id,'平台部','已打码内容',
                      :ciphertext,:biz_id,
                      true,'queued'
                    )
                    """
                ),
                {
                    "batch_no": BATCH_NO,
                    "app_id": APP_ID,
                    "ciphertext": b"ciphertext-only",
                    "biz_id": vendor_test_uat_biz_id(BATCH_ID),
                },
            )
        assert (
            await repository.expire_uat_if_stale(
                BATCH_ID,
                safe_code="UAT_ACCEPTANCE_EXPIRED",
            )
            is None
        )
        recovered, changed = await VendorTestUatReconciler(repository).recover(
            batch_running,
        )
        assert changed is True
        assert recovered.status == "running"
        assert recovered.batch_no == BATCH_NO
    finally:
        await cleanup()
        if principal is not None:
            await _delete_principal(engine, principal)
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_postgres_control_claim_has_one_winner_and_terminal_replay() -> None:
    database_url = make_url(os.environ["VENDOR_UAT_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    repository = SqlVendorTestOperationRepository(settings)
    engine = create_async_engine(database_url)
    principal: SecurityPrincipal | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE object_type='vendor_test_operation'
                      AND object_id=:control_id
                    """
                ),
                {"control_id": CONTROL_ID},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_operation WHERE id=:control_id"),
                {"control_id": CONTROL_ID},
            )

    try:
        await cleanup()
        principal = await _create_principal(engine, "vendor-uat-pg-control")
        requested = await repository.reserve_start(
            CONTROL_ID,
            "pause",
            principal=principal,
            conflicting_types=frozenset({"pause", "reset_configuration"}),
        )
        assert requested.status == "requested"

        claims = await asyncio.gather(
            repository.claim_control_running(CONTROL_ID),
            repository.claim_control_running(CONTROL_ID),
        )

        winners = [record for record in claims if record is not None]
        assert len(winners) == 1
        assert winners[0].status == "running"
        assert winners[0].operation_type == "pause"

        completed = await repository.complete(
            CONTROL_ID,
            status="succeeded",
            safe_code=None,
        )
        assert completed.status == "succeeded"
        assert completed.completed_at is not None
        assert await repository.claim_control_running(CONTROL_ID) is None

        terminal = await repository.get(CONTROL_ID)
        assert terminal == completed
        replayed = await repository.complete(
            CONTROL_ID,
            status="succeeded",
            safe_code=None,
        )
        assert replayed == completed
    finally:
        await cleanup()
        if principal is not None:
            await _delete_principal(engine, principal)
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_postgres_claim_wins_after_requested_reconcile_snapshot() -> None:
    database_url = make_url(os.environ["VENDOR_UAT_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    repository = SqlVendorTestOperationRepository(settings)
    engine = create_async_engine(database_url)
    principal: SecurityPrincipal | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE object_type='vendor_test_operation'
                      AND object_id=:control_id
                    """
                ),
                {"control_id": CONTROL_RACE_ID},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_operation WHERE id=:control_id"),
                {"control_id": CONTROL_RACE_ID},
            )

    snapshot_read = asyncio.Event()
    claim_finished = asyncio.Event()

    async def reconcile_from_requested_snapshot() -> object:
        snapshot = next(
            record
            for record in await repository.pending()
            if record.operation_id == CONTROL_RACE_ID
        )
        assert snapshot.status == "requested"
        snapshot_read.set()
        await claim_finished.wait()
        return await repository.fail_unclaimed_control(
            CONTROL_RACE_ID,
            safe_code="CONTROL_OPERATION_NOT_FOUND",
        )

    async def background_claim() -> object:
        await snapshot_read.wait()
        claimed = await repository.claim_control_running(CONTROL_RACE_ID)
        claim_finished.set()
        return claimed

    try:
        await cleanup()
        principal = await _create_principal(engine, "vendor-uat-pg-race")
        await repository.reserve_start(
            CONTROL_RACE_ID,
            "reset_configuration",
            principal=principal,
            conflicting_types=frozenset({"reset_configuration"}),
        )

        failed, claimed = await asyncio.gather(
            reconcile_from_requested_snapshot(),
            background_claim(),
        )

        assert failed is None
        assert claimed is not None
        assert claimed.status == "running"
        current = await repository.get(CONTROL_RACE_ID)
        assert current is not None
        assert current.status == "running"
        assert current.safe_code is None
    finally:
        await cleanup()
        if principal is not None:
            await _delete_principal(engine, principal)
        await engine.dispose()
