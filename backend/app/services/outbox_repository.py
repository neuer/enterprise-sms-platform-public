"""事务性 Outbox PostgreSQL 仓储、租约 CAS、dead-letter 与审计。"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import current_correlation_id
from app.core.runtime_resources import database_engine
from app.services.outbox import (
    OutboxClaim,
    OutboxContractConflict,
    OutboxEventPage,
    OutboxEventRecord,
    OutboxEventSpec,
    OutboxLease,
    OutboxLeaseLost,
    OutboxStats,
    validate_spec,
)
from app.settings import Settings, get_settings

SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")


def _safe_error(value: str) -> str:
    return value if SAFE_ERROR.fullmatch(value) else "OutboxError"


def _args(value: Any) -> tuple[str | int, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, (str, int)) or isinstance(item, bool) for item in value
    ):
        raise ValueError("invalid persisted outbox args")
    return tuple(value)


async def enqueue_outbox(
    connection: AsyncConnection,
    spec: OutboxEventSpec,
    *,
    available_delay_seconds: int = 0,
) -> UUID:
    """在调用方业务事务中插入事件；同 dedup_key 必须保持完全相同合同。"""

    validate_spec(spec)
    if not 0 <= available_delay_seconds <= 604800:
        raise ValueError("invalid outbox availability delay")
    event_id = uuid4()
    correlation_id = spec.correlation_id or current_correlation_id()
    assert correlation_id is not None
    result = await connection.execute(
        text(
            """
            INSERT INTO outbox_event(
              id,correlation_id,dedup_key,event_type,aggregate_type,aggregate_id,
              task_name,queue,args,max_attempts,next_attempt_at
            ) VALUES(
              :id,:correlation_id,:dedup_key,:event_type,:aggregate_type,:aggregate_id,
              :task_name,:queue,CAST(:args AS jsonb),:max_attempts,
              now()+make_interval(secs=>:available_delay_seconds)
            )
            ON CONFLICT(dedup_key) DO UPDATE
              SET dedup_key=outbox_event.dedup_key
            RETURNING id,event_type,aggregate_type,aggregate_id,
              task_name,queue,args,max_attempts,correlation_id
            """
        ),
        {
            "id": event_id,
            "correlation_id": correlation_id,
            "dedup_key": spec.dedup_key,
            "event_type": spec.event_type,
            "aggregate_type": spec.aggregate_type,
            "aggregate_id": spec.aggregate_id,
            "task_name": spec.task_name,
            "queue": spec.queue,
            "args": json.dumps(spec.args, separators=(",", ":")),
            "max_attempts": spec.max_attempts,
            "available_delay_seconds": available_delay_seconds,
        },
    )
    row = result.mappings().one()
    persisted = (
        str(row["event_type"]),
        str(row["aggregate_type"]),
        str(row["aggregate_id"]),
        str(row["task_name"]),
        str(row["queue"]),
        _args(row["args"]),
        int(row["max_attempts"]),
    )
    expected = (
        spec.event_type,
        spec.aggregate_type,
        spec.aggregate_id,
        spec.task_name,
        spec.queue,
        spec.args,
        spec.max_attempts,
    )
    if persisted != expected:
        raise OutboxContractConflict("outbox dedup key contract changed")
    return UUID(str(row["id"]))


class SqlOutboxRepository:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        pooled: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.pooled = pooled

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def lease_due(self, *, limit: int, lease_seconds: int) -> list[OutboxLease]:
        if not 1 <= limit <= 1000 or lease_seconds < 5:
            raise ValueError("invalid outbox lease request")
        async with self._engine().begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state='dead',lease_id=NULL,lease_expires_at=NULL,
                      failure_count=failure_count+
                        CASE WHEN state='pending' THEN 0 ELSE 1 END,
                      last_error=COALESCE(last_error,'AttemptsExhausted'),
                      updated_at=now()
                    WHERE state IN ('pending','leased','published','processing')
                      AND attempts>=max_attempts
                      AND (
                        state='pending'
                        OR lease_expires_at IS NULL
                        OR lease_expires_at<=now()
                      )
                    """
                )
            )
            result = await connection.execute(
                text(
                    """
                    WITH due AS (
                      SELECT id FROM outbox_event
                      WHERE attempts<max_attempts
                        AND (
                          (state='pending' AND next_attempt_at<=now())
                          OR (
                            state IN ('leased','published','processing')
                            AND lease_expires_at<=now()
                          )
                        )
                      ORDER BY next_attempt_at,created_at
                      FOR UPDATE SKIP LOCKED
                      LIMIT :limit
                    )
                    UPDATE outbox_event event SET
                      state='leased',
                      attempts=event.attempts+1,
                      failure_count=event.failure_count+
                        CASE WHEN event.state='pending' THEN 0 ELSE 1 END,
                      lease_id=gen_random_uuid(),
                      lease_expires_at=now()+make_interval(secs=>:lease_seconds),
                      next_attempt_at=now()+make_interval(
                        secs=>LEAST(3600,5*(2^LEAST(event.attempts,9)))::integer
                      ),
                      updated_at=now()
                    FROM due WHERE event.id=due.id
                    RETURNING event.id,event.lease_id,event.event_type,
                      event.task_name,event.queue,event.args,event.attempts,
                      event.correlation_id
                    """
                ),
                {"limit": limit, "lease_seconds": lease_seconds},
            )
            return [
                OutboxLease(
                    UUID(str(row["id"])),
                    UUID(str(row["lease_id"])),
                    str(row["event_type"]),
                    str(row["task_name"]),
                    str(row["queue"]),
                    _args(row["args"]),
                    int(row["attempts"]),
                    UUID(str(row["correlation_id"])),
                )
                for row in result.mappings()
            ]

    async def _lease_update(
        self,
        statement: str,
        *,
        event_id: UUID,
        lease_id: UUID,
        params: dict[str, object] | None = None,
    ) -> None:
        values: dict[str, object] = {"event_id": event_id, "lease_id": lease_id}
        values.update(params or {})
        async with self._engine().begin() as connection:
            result = await connection.execute(text(statement), values)
            if result.rowcount != 1:
                raise OutboxLeaseLost("outbox fencing token lost")

    async def mark_published(self, event_id: UUID, lease_id: UUID) -> None:
        async with self._engine().begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state='published',published_at=now(),updated_at=now()
                    WHERE id=:event_id AND state='leased' AND lease_id=:lease_id
                    """
                ),
                {"event_id": event_id, "lease_id": lease_id},
            )
            if result.rowcount == 1:
                return
            state = (
                await connection.execute(
                    text("SELECT state FROM outbox_event WHERE id=:event_id"),
                    {"event_id": event_id},
                )
            ).scalar_one_or_none()
            if state not in {"processing", "completed"}:
                raise OutboxLeaseLost("outbox fencing token lost")

    async def mark_publish_failed(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None:
        async with self._engine().begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state=CASE
                        WHEN attempts>=max_attempts THEN 'dead'
                        ELSE 'pending'
                      END,
                      lease_id=NULL,lease_expires_at=NULL,
                      failure_count=failure_count+1,
                      last_error=:error,updated_at=now()
                    WHERE id=:event_id AND state='leased' AND lease_id=:lease_id
                    """
                ),
                {
                    "event_id": event_id,
                    "lease_id": lease_id,
                    "error": _safe_error(error_type),
                },
            )
            if result.rowcount == 1:
                return
            state = (
                await connection.execute(
                    text("SELECT state FROM outbox_event WHERE id=:event_id"),
                    {"event_id": event_id},
                )
            ).scalar_one_or_none()
            if state not in {"processing", "completed"}:
                raise OutboxLeaseLost("outbox fencing token lost")

    async def claim_execution(
        self,
        event_id: UUID,
        *,
        lease_seconds: int,
    ) -> OutboxClaim | None:
        if lease_seconds < 15:
            raise ValueError("invalid outbox execution lease")
        execution_lease = uuid4()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state='processing',lease_id=:execution_lease,
                      lease_expires_at=now()+make_interval(secs=>:lease_seconds),
                      updated_at=now()
                    WHERE id=:event_id
                      AND state IN ('leased','published')
                    RETURNING id,lease_id,event_type,args,correlation_id
                    """
                ),
                {
                    "event_id": event_id,
                    "execution_lease": execution_lease,
                    "lease_seconds": lease_seconds,
                },
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return OutboxClaim(
                UUID(str(row["id"])),
                UUID(str(row["lease_id"])),
                str(row["event_type"]),
                _args(row["args"]),
                UUID(str(row["correlation_id"])),
            )

    async def heartbeat(
        self,
        event_id: UUID,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool:
        async with self._engine().begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      lease_expires_at=now()+make_interval(secs=>:lease_seconds),
                      updated_at=now()
                    WHERE id=:event_id AND state='processing' AND lease_id=:lease_id
                    """
                ),
                {
                    "event_id": event_id,
                    "lease_id": lease_id,
                    "lease_seconds": lease_seconds,
                },
            )
            return int(result.rowcount or 0) == 1

    async def complete(self, event_id: UUID, lease_id: UUID) -> None:
        await self._lease_update(
            """
            UPDATE outbox_event SET
              state='completed',completed_at=now(),
              lease_id=NULL,lease_expires_at=NULL,last_error=NULL,updated_at=now()
            WHERE id=:event_id AND state='processing' AND lease_id=:lease_id
            """,
            event_id=event_id,
            lease_id=lease_id,
        )

    async def fail_execution(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None:
        await self._lease_update(
            """
            UPDATE outbox_event SET
              state=CASE WHEN attempts>=max_attempts THEN 'dead' ELSE 'pending' END,
              lease_id=NULL,lease_expires_at=NULL,
              failure_count=failure_count+1,
              last_error=:error,updated_at=now()
            WHERE id=:event_id AND state='processing' AND lease_id=:lease_id
            """,
            event_id=event_id,
            lease_id=lease_id,
            params={"error": _safe_error(error_type)},
        )

    async def stats(self) -> OutboxStats:
        async with self._engine().connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT
                          count(*) FILTER (
                            WHERE state IN ('pending','leased')
                          ) pending,
                          count(*) FILTER (WHERE state='published') published,
                          count(*) FILTER (WHERE state='processing') processing,
                          count(*) FILTER (WHERE state='dead') dead,
                          COALESCE(
                            sum(failure_count) FILTER (WHERE state<>'completed'),
                            0
                          )
                            failed_attempts,
                          COALESCE(EXTRACT(epoch FROM (
                            now()-min(created_at) FILTER (WHERE state<>'completed')
                          ))::integer,0) oldest_age_seconds
                        FROM outbox_event
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            return OutboxStats(
                int(row["pending"]),
                int(row["published"]),
                int(row["processing"]),
                int(row["dead"]),
                int(row["failed_attempts"]),
                max(0, int(row["oldest_age_seconds"])),
            )

    async def list_events(
        self,
        state: str | None,
        page: int,
        page_size: int,
    ) -> OutboxEventPage:
        """按状态分页列出事件元数据；dead 优先，永不返回 args/dedup_key。"""
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("invalid outbox event page")
        where = "(CAST(:state AS varchar(16)) IS NULL OR state=CAST(:state AS varchar(16)))"
        params = {"state": state, "limit": page_size, "offset": (page - 1) * page_size}
        async with self._engine().connect() as connection:
            count = await connection.execute(
                text("SELECT count(*) FROM outbox_event WHERE " + where),
                params,
            )
            result = await connection.execute(
                text(
                    "SELECT id,event_type,aggregate_type,aggregate_id,task_name,queue,"
                    "state,attempts,max_attempts,failure_count,last_error,"
                    "next_attempt_at,created_at,updated_at "
                    "FROM outbox_event WHERE "
                    + where
                    + " ORDER BY (state='dead') DESC,updated_at DESC,id "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            return OutboxEventPage(
                tuple(
                    OutboxEventRecord(
                        UUID(str(row["id"])),
                        str(row["event_type"]),
                        str(row["aggregate_type"]),
                        str(row["aggregate_id"]),
                        str(row["task_name"]),
                        str(row["queue"]),
                        str(row["state"]),
                        int(row["attempts"]),
                        int(row["max_attempts"]),
                        int(row["failure_count"]),
                        None if row["last_error"] is None else str(row["last_error"]),
                        row["next_attempt_at"],
                        row["created_at"],
                        row["updated_at"],
                    )
                    for row in result.mappings()
                ),
                int(count.scalar_one()),
                page,
                page_size,
            )

    async def retry_dead(
        self,
        event_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> bool:
        async with self._engine().begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET
                      state='pending',attempts=0,next_attempt_at=now(),
                      lease_id=NULL,lease_expires_at=NULL,last_error=NULL,
                      updated_at=now()
                    WHERE id=:event_id AND state='dead'
                    RETURNING aggregate_type,aggregate_id
                    """
                ),
                {"event_id": event_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                return False
            await connection.execute(
                text(
                    """
                    INSERT INTO audit_log(
                      actor,actor_subject_kind,actor_account_id,actor_identity_id,
                      role,action,object_type,object_id,after_val
                    ) VALUES(
                      :actor,'human',:account_id,:identity_id,:role,
                      'outbox_retry','outbox_event',:event_id,
                      jsonb_build_object(
                        'actor_account_id',CAST(:account_id AS bigint),
                        'actor_identity_id',CAST(:identity_id AS bigint),
                        'aggregate_type',CAST(:aggregate_type AS text),
                        'aggregate_id',CAST(:aggregate_id AS text)
                      )
                    )
                    """
                ),
                {
                    "actor": principal.login_name,
                    "account_id": principal.account_id,
                    "identity_id": principal.identity_id,
                    "role": principal.role,
                    "event_id": str(event_id),
                    "aggregate_type": str(row["aggregate_type"]),
                    "aggregate_id": str(row["aggregate_id"]),
                },
            )
            return True
