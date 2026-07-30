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
from app.services.housekeeping import LifecyclePolicy
from app.services.housekeeping_repository import SqlHousekeepingRepository
from app.services.import_repository import (
    ImportReservation,
    ImportStateConflict,
    SqlImportRepository,
    consume_import_reservation,
)
from app.services.imports import ImportPhone, ImportResult

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_import_reservation_is_concurrent_recoverable_and_batch_bound(
    tmp_path: Any,
) -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(database_url=database_url, import_storage_dir=tmp_path),
    )
    engine = create_async_engine(database_url)
    repository = SqlImportRepository(settings)
    housekeeping = SqlHousekeepingRepository(settings)
    nonce = uuid4().hex
    login = f"import-{nonce[:16]}"
    batch_no = nonce[:32]
    account_id: int | None = None
    identity_id: int | None = None
    import_id: str | None = None
    batch_id: int | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            if import_id is not None:
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
                    text(
                        "DELETE FROM import_task "
                        "WHERE import_id=CAST(:import_id AS uuid)"
                    ),
                    {"import_id": import_id},
                )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": account_id},
            )
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no=:batch_no"),
                {"batch_no": batch_no},
            )
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:identity_id"),
                {"identity_id": identity_id},
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:account_id"),
                {"account_id": account_id},
            )

    try:
        async with engine.begin() as connection:
            account_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(display_name,dept,role)
                            VALUES(:login,'平台部','operator') RETURNING id
                            """
                        ),
                        {"login": login},
                    )
                ).scalar_one()
            )
            provider_id = int(
                (
                    await connection.execute(
                        text("SELECT id FROM auth_provider WHERE code='local'")
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
                              :account_id,:provider_id,:login,:login,:subject
                            ) RETURNING id
                            """
                        ),
                        {
                            "account_id": account_id,
                            "provider_id": provider_id,
                            "login": login,
                            "subject": f"local:{login}",
                        },
                    )
                ).scalar_one()
            )
        principal = SecurityPrincipal(
            account_id,
            identity_id,
            login,
            "平台部",
            "operator",
        )
        stored = await repository.persist(
            ImportResult(
                [ImportPhone(b"ciphertext-only", "a" * 64, "138****8000", 1, 2)],
                [],
            ),
            principal=principal,
            filename="phones.csv",
            expire_hours=6,
        )
        import_id = stored.import_id

        first = await repository.reserve(import_id, principal=principal)
        async with engine.begin() as connection:
            task_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            UPDATE import_task
                            SET expires_at=now()-interval '1 second'
                            WHERE import_id=CAST(:import_id AS uuid)
                            RETURNING id
                            """
                        ),
                        {"import_id": import_id},
                    )
                ).scalar_one()
            )
        assert task_id not in {item.id for item in await housekeeping.expired_imports()}
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE import_task SET expires_at=now()+interval '6 hours'
                    WHERE id=:task_id
                    """
                ),
                {"task_id": task_id},
            )
        assert first.phones and await repository.release(
            first.reservation_id,
            principal=principal,
        )
        retried = await repository.reserve(import_id, principal=principal)
        assert retried.reservation_id != first.reservation_id
        assert await repository.release(retried.reservation_id, principal=principal)

        results = await asyncio.gather(
            repository.reserve(import_id, principal=principal),
            SqlImportRepository(settings).reserve(import_id, principal=principal),
            return_exceptions=True,
        )
        winners = [item for item in results if isinstance(item, ImportReservation)]
        conflicts = [item for item in results if isinstance(item, ImportStateConflict)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        crashed = winners[0]

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE import_task
                    SET reservation_expires_at=now()-interval '1 second'
                    WHERE import_id=CAST(:import_id AS uuid)
                      AND reservation_id=CAST(:reservation_id AS uuid)
                    """
                ),
                {
                    "import_id": import_id,
                    "reservation_id": str(crashed.reservation_id),
                },
            )
        recovered = await SqlImportRepository(settings).reserve(
            import_id,
            principal=principal,
        )
        assert recovered.reservation_id != crashed.reservation_id

        async with engine.begin() as connection:
            batch_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_batch(
                              batch_no,channel,creator,creator_account_id,
                              creator_identity_id,dept,content,send_content_enc,
                              status,total
                            ) VALUES(
                              :batch_no,'web',:creator,:account_id,:identity_id,
                              '平台部','维护通知',:ciphertext,'queued',1
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_no": batch_no,
                            "creator": login,
                            "account_id": account_id,
                            "identity_id": identity_id,
                            "ciphertext": b"ciphertext-only",
                        },
                    )
                ).scalar_one()
            )
            await consume_import_reservation(
                connection,
                reservation_id=recovered.reservation_id,
                batch_id=batch_id,
                principal=principal,
            )

        replay = await SqlImportRepository(settings).reserve(
            import_id,
            principal=principal,
        )
        assert replay.consumed_batch_no == batch_no
        assert replay.reservation_id == recovered.reservation_id
        assert replay.phones == ()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE import_task SET expires_at=now()-interval '1 second'
                    WHERE import_id=CAST(:import_id AS uuid)
                    """
                ),
                {"import_id": import_id},
            )
        expired = await housekeeping.expired_imports()
        await housekeeping.cleanup(
            LifecyclePolicy(90, 90, 30),
            tuple(item.id for item in expired),
        )
        retained_replay = await SqlImportRepository(settings).reserve(
            import_id,
            principal=principal,
        )
        assert retained_replay.consumed_batch_no == batch_no
        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        """
                        SELECT state,reservation_id,reserved_by_account_id,
                          consumed_batch_id,consumed_at IS NOT NULL consumed,
                          payload_purged_at IS NOT NULL payload_purged,
                          (SELECT count(*) FROM import_phone p
                           WHERE p.import_task_id=import_task.id) phone_count
                        FROM import_task
                        WHERE import_id=CAST(:import_id AS uuid)
                        """
                    ),
                    {"import_id": import_id},
                )
            ).mappings().one()
        assert state["state"] == "consumed"
        assert int(state["reserved_by_account_id"]) == account_id
        assert int(state["consumed_batch_id"]) == batch_id
        assert state["consumed"] is True
        assert state["payload_purged"] is True
        assert int(state["phone_count"]) == 0
    finally:
        await cleanup()
        await engine.dispose()
