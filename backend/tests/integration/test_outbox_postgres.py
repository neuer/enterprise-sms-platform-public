from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from app.services.outbox import (
    OutboxClaim,
    OutboxDispatcher,
    OutboxEventSpec,
    OutboxExecutor,
    OutboxLeaseLost,
)
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class FailingPublisher:
    async def publish(self, _event: object) -> None:
        raise ConnectionError("synthetic broker outage")


@pytest.mark.asyncio
async def test_outbox_concurrency_fencing_recovery_and_privileges() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlOutboxRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    raw_nonce = uuid4().hex
    nonce = "-".join(raw_nonce[index : index + 4] for index in range(0, 32, 4))
    phoneish_batch_id = "13800138000" + "a" * 21
    dedup_prefix = f"integration:{nonce}"
    login_name = f"outbox-{nonce[:12]}"
    account_id: int | None = None
    identity_id: int | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE action='outbox_retry'
                      AND object_id IN (
                        SELECT CAST(id AS text) FROM outbox_event
                        WHERE dedup_key LIKE :dedup_pattern
                      )
                    """
                ),
                {"dedup_pattern": f"{dedup_prefix}%"},
            )
            await connection.execute(
                text("DELETE FROM outbox_event WHERE dedup_key LIKE :dedup_pattern"),
                {"dedup_pattern": f"{dedup_prefix}%"},
            )
            if identity_id is not None:
                await connection.execute(
                    text("DELETE FROM auth_identity WHERE id=:identity_id"),
                    {"identity_id": identity_id},
                )
            if account_id is not None:
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=:account_id"),
                    {"account_id": account_id},
                )

    try:
        await cleanup()
        async with engine.begin() as connection:
            event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="batch.ready",
                    aggregate_type="sms_batch",
                    aggregate_id=phoneish_batch_id,
                    task_name="app.tasks.send.process_batch",
                    queue="realtime",
                    args=(phoneish_batch_id,),
                    dedup_key=f"{dedup_prefix}:concurrency",
                ),
            )

        first, second = await asyncio.gather(
            repository.lease_due(limit=1, lease_seconds=15),
            repository.lease_due(limit=1, lease_seconds=15),
        )
        leases = [*first, *second]
        assert len(leases) == 1
        original_lease = leases[0]
        assert original_lease.event_id == event_id

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:event_id
                    """
                ),
                {"event_id": event_id},
            )
        replacement = (await repository.lease_due(limit=1, lease_seconds=15))[0]
        assert replacement.lease_id != original_lease.lease_id
        with pytest.raises(OutboxLeaseLost):
            await repository.mark_published(
                original_lease.event_id,
                original_lease.lease_id,
            )

        effect_started = asyncio.Event()
        release_effect = asyncio.Event()
        effects = 0

        async def effect(claim: OutboxClaim) -> int:
            nonlocal effects
            assert claim.args == (phoneish_batch_id,)
            effects += 1
            effect_started.set()
            await release_effect.wait()
            return 1

        first_execution = asyncio.create_task(
            OutboxExecutor(repository, lease_seconds=15).run(
                event_id,
                expected_type="batch.ready",
                effect=effect,
            )
        )
        await effect_started.wait()
        duplicate_result = await OutboxExecutor(repository, lease_seconds=15).run(
            event_id,
            expected_type="batch.ready",
            effect=effect,
        )
        release_effect.set()
        assert await first_execution == 1
        assert duplicate_result == 0
        assert effects == 1
        # 消费者可能在 send_task 返回后、dispatcher 回写 published 前完成。
        # 该合法竞态不得把已经完成的事件误判成 fencing 失败。
        await repository.mark_published(replacement.event_id, replacement.lease_id)
        await repository.mark_publish_failed(
            replacement.event_id,
            replacement.lease_id,
            "LatePublisherError",
        )

        async with engine.begin() as connection:
            failed_event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="batch.ready",
                    aggregate_type="sms_batch",
                    aggregate_id=f"BATCH-FAIL-{nonce}",
                    task_name="app.tasks.send.process_batch",
                    queue="realtime",
                    args=(f"BATCH-FAIL-{nonce}",),
                    dedup_key=f"{dedup_prefix}:broker-failure",
                ),
            )
        assert (
            await OutboxDispatcher(repository, FailingPublisher()).dispatch_once()
            == 0
        )
        async with engine.connect() as connection:
            failed_row = (
                await connection.execute(
                    text(
                        """
                        SELECT state,last_error,attempts,failure_count
                        FROM outbox_event
                        WHERE id=:event_id
                        """
                    ),
                    {"event_id": failed_event_id},
                )
            ).mappings().one()
        assert failed_row["state"] == "pending"
        assert failed_row["last_error"] == "ConnectionError"
        assert int(failed_row["attempts"]) == 1
        assert int(failed_row["failure_count"]) == 1

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO outbox_event(
                          id,dedup_key,event_type,aggregate_type,aggregate_id,
                          task_name,queue,args
                        ) VALUES(
                          :id,:dedup_key,'batch.ready','sms_batch','unsafe',
                          'app.tasks.send.process_batch','realtime',
                          CAST(:args AS jsonb)
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "dedup_key": f"{dedup_prefix}:pii-rejected",
                        "args": '["13800138000"]',
                    },
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO outbox_event(
                          id,dedup_key,event_type,aggregate_type,aggregate_id,
                          task_name,queue,args
                        ) VALUES(
                          :id,:dedup_key,'batch.ready','sms_batch','unsafe',
                          'app.tasks.send.process_batch','realtime',
                          CAST(:args AS jsonb)
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "dedup_key": f"{dedup_prefix}:object-rejected",
                        "args": '[{"batch_no":"BATCH"}]',
                    },
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO outbox_event(
                          id,dedup_key,event_type,aggregate_type,aggregate_id,
                          task_name,queue,args
                        ) VALUES(
                          :id,:dedup_key,'batch.ready','sms_batch','unsafe',
                          'app.tasks.housekeeping.cleanup','realtime','[]'::jsonb
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "dedup_key": f"{dedup_prefix}:task-rejected",
                    },
                )

        async with engine.begin() as connection:
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
                        {"login": login_name},
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
                            "login": login_name,
                            "subject": f"local:{login_name}",
                        },
                    )
                ).scalar_one()
            )
            retry_event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="batch.ready",
                    aggregate_type="sms_batch",
                    aggregate_id=f"BATCH-RETRY-{nonce}",
                    task_name="app.tasks.send.process_batch",
                    queue="realtime",
                    args=(f"BATCH-RETRY-{nonce}",),
                    dedup_key=f"{dedup_prefix}:manual-retry",
                ),
            )
            await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET state='dead',attempts=max_attempts
                    WHERE id=:event_id
                    """
                ),
                {"event_id": retry_event_id},
            )

        principal = SecurityPrincipal(
            account_id,
            identity_id,
            login_name,
            "平台部",
            "admin",
        )
        with audit_principal_scope(principal), correlation_scope(uuid4()):
            assert await repository.retry_dead(retry_event_id, principal=principal)
        async with engine.connect() as connection:
            audit_row = (
                await connection.execute(
                    text(
                        """
                        SELECT actor_account_id,actor_identity_id,after_val
                        FROM audit_log
                        WHERE action='outbox_retry' AND object_id=:event_id
                        ORDER BY id DESC LIMIT 1
                        """
                    ),
                    {"event_id": str(retry_event_id)},
                )
            ).mappings().one()
            privilege_row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                          (
                            has_table_privilege(
                              'sms_scheduler','outbox_event','SELECT'
                            )
                            AND has_table_privilege(
                              'sms_scheduler','outbox_event','INSERT'
                            )
                            AND has_table_privilege(
                              'sms_scheduler','outbox_event','UPDATE'
                            )
                          ) can_write,
                          has_table_privilege(
                            'sms_scheduler','outbox_event','DELETE'
                          ) can_delete,
                          has_table_privilege(
                            'sms_scheduler','outbox_event','TRUNCATE'
                          ) can_truncate
                        """
                    )
                )
            ).mappings().one()
        assert int(audit_row["actor_account_id"]) == account_id
        assert int(audit_row["actor_identity_id"]) == identity_id
        assert str(account_id) in str(audit_row["after_val"])
        assert privilege_row["can_write"] is True
        assert privilege_row["can_delete"] is False
        assert privilege_row["can_truncate"] is False
    finally:
        await cleanup()
        await engine.dispose()
        await close_runtime_resources()
