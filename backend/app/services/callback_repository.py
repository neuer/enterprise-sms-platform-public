"""callback_task 无 PII 引用生产、投递状态与管理查询仓储。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import current_correlation_id
from app.core.runtime_resources import database_engine
from app.services.callback import (
    BatchFinishedData,
    CallbackMaterial,
    CallbackMessage,
    CallbackTaskRef,
    MessageReportData,
)
from app.services.callback_authority import CallbackAuthorityBusy
from app.services.callback_worker import CallbackClaim, CallbackLeaseLost
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.settings import Settings, get_settings

SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def report_event_key(
    *,
    message_id: int,
    message_created_at: datetime,
    custom_id: str,
    report_status: int,
    report_desc: str,
    report_time: datetime,
) -> str:
    """用无 PII 规范字段生成稳定报告事件 SHA-256。"""

    def timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("report event timestamps must be timezone-aware")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    values = (
        str(message_id),
        timestamp(message_created_at),
        custom_id.strip(),
        str(report_status),
        report_desc,
        timestamp(report_time),
    )
    canonical = "".join(f"{len(value.encode('utf-8'))}:{value}" for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CallbackTaskNotFound(LookupError):
    pass


class CallbackRetryConflict(RuntimeError):
    pass


async def enqueue_batch_finished(
    connection: Any,
    batch_id: int,
    *,
    source_report_event_key: str | None = None,
) -> None:
    """按有效报告修订幂等创建 batch.finished 引用任务。"""

    correlation_id = current_correlation_id()
    assert correlation_id is not None
    inserted = await connection.execute(
        text(
            """
            WITH task_lock AS (
              SELECT pg_advisory_xact_lock(
                hashtextextended('batch.finished:' || CAST(:batch_id AS text),0)
              )
            ), candidate AS (
              SELECT b.id batch_id,b.app_id,a.callback_url,a.callback_secret_enc
              FROM sms_batch b JOIN app a ON a.id=b.app_id
              WHERE b.id=CAST(:batch_id AS bigint)
                AND b.status IN ('completed','cancelled','expired','rejected')
                AND a.status=1 AND a.callback_url IS NOT NULL
                AND a.callback_secret_enc IS NOT NULL
            )
            INSERT INTO callback_task(
              correlation_id,app_id,event,batch_id,source_report_event_key,url,
              callback_secret_enc,callback_secret_key_version,signature_version
            )
            SELECT :correlation_id,c.app_id,'batch.finished',c.batch_id,
              CAST(:source_report_event_key AS char(64)),c.callback_url,
              c.callback_secret_enc,
              ((get_byte(c.callback_secret_enc,0) << 8)
                + get_byte(c.callback_secret_enc,1))::smallint,
              1
            FROM candidate c,task_lock
            WHERE CAST(:source_report_event_key AS char(64)) IS NOT NULL
               OR NOT EXISTS (
                 SELECT 1 FROM callback_task t
                 WHERE t.event='batch.finished' AND t.batch_id=c.batch_id
                   AND t.source_report_event_key IS NULL
               )
            ON CONFLICT DO NOTHING
            RETURNING id,correlation_id
            """
        ),
        {
            "batch_id": str(batch_id),
            "source_report_event_key": source_report_event_key,
            "correlation_id": correlation_id,
        },
    )
    row = inserted.mappings().one_or_none()
    if row is not None:
        task_id = int(row["id"])
        await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="callback.ready",
                aggregate_type="callback_task",
                aggregate_id=str(task_id),
                task_name="app.tasks.deliver_callback",
                queue="callback",
                args=(int(task_id),),
                dedup_key=f"callback:{task_id}:attempt:0",
                correlation_id=UUID(str(row["correlation_id"])),
            ),
        )


async def enqueue_message_report(
    connection: Any,
    *,
    batch_id: int,
    message_id: int,
    created_at: datetime,
    event_key: str,
    message_status: str = "unknown",
    report_desc: str = "",
    report_time: datetime | None = None,
) -> None:
    """同一 app×batch×分钟最多向单任务追加 500 个消息复合引用。"""

    params = {
        "batch_id": batch_id,
        "message_id": message_id,
        "created_at": created_at,
        "event_key": event_key,
        "message_status": message_status,
        "report_desc": report_desc,
        "report_time": report_time or created_at,
    }
    await connection.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('message.report:' || CAST(:batch_id AS text),0))"
        ),
        {"batch_id": str(batch_id)},
    )
    inserted_event = await connection.execute(
        text(
            """
            INSERT INTO callback_report_event(
              event_key,batch_id,message_id,message_created_at,
              message_status,report_desc,report_time
            ) VALUES (
              CAST(:event_key AS char(64)),:batch_id,:message_id,:created_at,
              :message_status,:report_desc,:report_time
            ) ON CONFLICT(event_key) DO NOTHING RETURNING event_key
            """
        ),
        params,
    )
    if inserted_event.scalar_one_or_none() is None:
        return
    appended = await connection.execute(
        text(
            """
            UPDATE callback_task SET
              message_ids=array_append(message_ids,:message_id),
              message_times=array_append(message_times,:created_at),
              event_keys=array_append(event_keys,CAST(:event_key AS char(64)))
            WHERE id=(
              SELECT id FROM callback_task
              WHERE event='message.report' AND batch_id=:batch_id
                AND status='pending' AND retry_count=0
                AND created_at>=date_trunc('minute',now())
                AND cardinality(message_ids)<500
                AND EXISTS (
                  SELECT 1 FROM app a WHERE a.id=callback_task.app_id
                    AND a.status=1 AND a.callback_report_enabled=true
                    AND a.callback_url IS NOT NULL
                    AND a.callback_url=callback_task.url
                    AND a.callback_secret_enc=callback_task.callback_secret_enc
                )
              ORDER BY id DESC LIMIT 1 FOR UPDATE
            ) RETURNING id
            """
        ),
        params,
    )
    if appended.scalar_one_or_none() is not None:
        return
    correlation_id = current_correlation_id()
    assert correlation_id is not None
    created = await connection.execute(
        text(
            """
            INSERT INTO callback_task(
              correlation_id,app_id,event,batch_id,
              message_ids,message_times,event_keys,url,
              callback_secret_enc,callback_secret_key_version,signature_version
            )
            SELECT :correlation_id,a.id,'message.report',b.id,
              ARRAY[:message_id]::bigint[],ARRAY[:created_at]::timestamptz[],
              ARRAY[CAST(:event_key AS char(64))]::char(64)[],
              a.callback_url,a.callback_secret_enc,
              ((get_byte(a.callback_secret_enc,0) << 8)
                + get_byte(a.callback_secret_enc,1))::smallint,
              1
            FROM sms_batch b JOIN app a ON a.id=b.app_id
            WHERE b.id=:batch_id AND a.status=1
              AND a.callback_url IS NOT NULL
              AND a.callback_secret_enc IS NOT NULL
              AND a.callback_report_enabled=true
            RETURNING id,correlation_id
            """
        ),
        {**params, "correlation_id": correlation_id},
    )
    row = created.mappings().one_or_none()
    if row is not None:
        task_id = int(row["id"])
        await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="callback.ready",
                aggregate_type="callback_task",
                aggregate_id=str(task_id),
                task_name="app.tasks.deliver_callback",
                queue="callback",
                args=(int(task_id),),
                dedup_key=f"callback:{task_id}:attempt:0",
                correlation_id=UUID(str(row["correlation_id"])),
            ),
        )


def _safe_error(error: str | None) -> str | None:
    if error is None:
        return None
    return error if SAFE_ERROR.fullmatch(error) else "CallbackError"


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符（PostgreSQL 默认反斜杠转义）。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlCallbackRepository:
    """callback PostgreSQL 事实源；每次执行使用独立 UUID fencing token。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("callback"))

    async def callback_allow_cidrs(self) -> str:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='callback_allow_cidrs'")
                )
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    async def due_ids(self, limit: int = 100) -> list[int]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id FROM callback_task
                        WHERE status='pending'
                           OR (
                             status='retrying' AND lease_id IS NULL
                             AND next_retry_at<=now()
                           )
                           OR (
                             status='retrying' AND lease_id IS NOT NULL
                             AND lease_expires_at<=now()
                           )
                        ORDER BY COALESCE(
                          lease_expires_at,next_retry_at,created_at
                        ),id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                return [int(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def list_page(
        self,
        *,
        status: str | None,
        app_id: int | None,
        event: str | None,
        batch_no: str | None,
        page: int,
        size: int = 20,
    ) -> dict[str, object]:
        where = """
          (CAST(:status AS varchar(10)) IS NULL
            OR t.status=CAST(:status AS varchar(10)))
          AND (CAST(:app_id AS bigint) IS NULL
            OR t.app_id=CAST(:app_id AS bigint))
          AND (CAST(:event AS varchar(20)) IS NULL
            OR t.event=CAST(:event AS varchar(20)))
        """
        params: dict[str, object] = {
            "status": status,
            "app_id": app_id,
            "event": event,
            "limit": size,
            "offset": (page - 1) * size,
        }
        if batch_no is not None and batch_no.strip():
            where += " AND trim(b.batch_no) ILIKE :batch_no"
            params["batch_no"] = f"%{_escape_like(batch_no.strip())}%"
        source = (
            " FROM callback_task t JOIN app a ON a.id=t.app_id"
            " LEFT JOIN sms_batch b ON b.id=t.batch_id "
        )
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                total_result = await connection.execute(
                    text("SELECT count(*)" + source + "WHERE " + where),
                    params,
                )
                rows_result = await connection.execute(
                    text(
                        """
                        SELECT t.id,t.event_id,t.correlation_id,
                          t.app_id,a.name app_name,t.event,
                          trim(b.batch_no) batch_no,
                          cardinality(t.message_ids) reference_count,t.status,t.retry_count,
                          t.next_retry_at,t.lease_id,t.lease_expires_at,
                          t.takeover_count,
                          (t.lease_id IS NOT NULL AND t.lease_expires_at<=now()) stalled,
                          t.last_http_code,t.last_error,
                          t.created_at,t.finished_at
                        """
                        + source
                        + "WHERE "
                        + where
                        + " ORDER BY t.created_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                dead_result = await connection.execute(
                    text("SELECT count(*) FROM callback_task WHERE status='dead'"),
                )
                return {
                    "total": int(total_result.scalar_one()),
                    "dead_total": int(dead_result.scalar_one()),
                    "items": [dict(row) for row in rows_result.mappings()],
                }
        finally:
            await engine.dispose()

    async def manual_retry(
        self,
        task_id: int,
        *,
        principal: SecurityPrincipal,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE callback_task t SET status='pending',retry_count=0,
                          next_retry_at=NULL,last_http_code=NULL,last_error=NULL,
                          lease_id=NULL,lease_expires_at=NULL,finished_at=NULL
                        FROM app a
                        WHERE t.id=:task_id AND t.status='dead'
                          AND t.last_error IS DISTINCT FROM 'CallbackConfigRevoked'
                          AND a.id=t.app_id AND a.status=1
                          AND a.callback_url=t.url
                          AND a.callback_secret_enc=t.callback_secret_enc
                          AND (
                            t.event<>'message.report'
                            OR a.callback_report_enabled=true
                          )
                        RETURNING t.id
                        """
                    ),
                    {"task_id": task_id},
                )
                if result.scalar_one_or_none() is None:
                    status_result = await connection.execute(
                        text("SELECT status FROM callback_task WHERE id=:task_id"),
                        {"task_id": task_id},
                    )
                    if status_result.scalar_one_or_none() is None:
                        raise CallbackTaskNotFound("回调任务不存在")
                    raise CallbackRetryConflict("仅 dead 回调可手动重推")
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        action="callback_retry",
                        object_type="callback_task",
                        object_id=str(task_id),
                        after={"status": "pending"},
                    ),
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO worker_lease_event(
                          task_kind,task_id,event_type,lease_id
                        ) VALUES ('callback',:task_id,'manual_retry',NULL)
                        """
                    ),
                    {"task_id": task_id},
                )
        finally:
            await engine.dispose()

    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int = 30,
    ) -> CallbackClaim | None:
        if lease_seconds < 3:
            raise ValueError("callback lease must be at least 3 seconds")
        lease_id = uuid4()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH candidate AS (
                          SELECT id,(lease_id IS NOT NULL) takeover
                          FROM callback_task
                          WHERE id=:task_id AND (
                            status='pending'
                            OR (
                              status='retrying' AND lease_id IS NULL
                              AND next_retry_at<=now()
                            )
                            OR (
                              status='retrying' AND lease_id IS NOT NULL
                              AND lease_expires_at<=now()
                            )
                          )
                          FOR UPDATE
                        ), claimed AS (
                          UPDATE callback_task task SET
                            status='retrying',next_retry_at=NULL,
                            lease_id=:lease_id,
                            lease_expires_at=now()+make_interval(
                              secs=>:lease_seconds
                            ),
                            takeover_count=task.takeover_count+
                              CASE WHEN candidate.takeover THEN 1 ELSE 0 END
                          FROM candidate WHERE task.id=candidate.id
                          RETURNING task.id,task.app_id,task.event_id,
                            task.correlation_id,task.event,
                            task.retry_count,task.lease_id,task.lease_expires_at,
                            candidate.takeover
                        )
                        SELECT * FROM claimed
                        """
                    ),
                    {
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "lease_seconds": lease_seconds,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                event_type = "takeover" if bool(row["takeover"]) else "acquired"
                await connection.execute(
                    text(
                        """
                        INSERT INTO worker_lease_event(
                          task_kind,task_id,event_type,lease_id
                        ) VALUES ('callback',:task_id,:event_type,:lease_id)
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": event_type,
                        "lease_id": lease_id,
                    },
                )
                return CallbackClaim(
                    int(row["id"]),
                    int(row["app_id"]),
                    UUID(str(row["event_id"])),
                    str(row["event"]),
                    int(row["retry_count"]),
                    UUID(str(row["lease_id"])),
                    row["lease_expires_at"],
                    UUID(str(row["correlation_id"])),
                )
        finally:
            await engine.dispose()

    async def heartbeat(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool:
        engine = self._engine()
        renewed = False
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE callback_task
                        SET lease_expires_at=now()+make_interval(
                          secs=>:lease_seconds
                        )
                        WHERE id=:task_id AND status='retrying'
                          AND lease_id=:lease_id AND lease_expires_at>now()
                        RETURNING id
                        """
                    ),
                    {
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "lease_seconds": lease_seconds,
                    },
                )
                renewed = result.scalar_one_or_none() is not None
                if renewed:
                    await connection.execute(
                        text(
                            """
                            UPDATE callback_authority_lease
                            SET expires_at=now()+make_interval(secs=>:lease_seconds)
                            WHERE task_id=:task_id AND lease_id=:lease_id
                            """
                        ),
                        {
                            "task_id": task_id,
                            "lease_id": lease_id,
                            "lease_seconds": lease_seconds,
                        },
                    )
        finally:
            await engine.dispose()
        if not renewed:
            await self._record_lease_event(task_id, "heartbeat_lost", lease_id)
        return renewed

    async def mark_done(
        self,
        task_id: int,
        lease_id: UUID,
        http_code: int,
    ) -> None:
        await self._state_update(
            """
            UPDATE callback_task SET status='done',finished_at=now(),next_retry_at=NULL,
              lease_id=NULL,lease_expires_at=NULL,
              last_http_code=:http_code,last_error=NULL
            WHERE id=:task_id AND status='retrying' AND lease_id=:lease_id
              AND lease_expires_at>now()
            RETURNING id
            """,
            {"task_id": task_id, "lease_id": lease_id, "http_code": http_code},
        )

    async def mark_authority_busy(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
    ) -> None:
        """同一应用的并发投递只延期，不消耗外部投递重试次数。"""

        engine = self._engine()
        deferred = False
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE callback_task SET status='retrying',
                          next_retry_at=now()+make_interval(secs=>:delay_s),
                          lease_id=NULL,lease_expires_at=NULL,
                          last_http_code=NULL,last_error='CallbackAuthorityBusy'
                        WHERE id=:task_id AND status='retrying'
                          AND retry_count=:retry_count
                          AND lease_id=:lease_id AND lease_expires_at>now()
                        RETURNING id
                        """
                    ),
                    {
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "retry_count": retry_count,
                        "delay_s": delay_s,
                    },
                )
                deferred = result.scalar_one_or_none() is not None
                if deferred:
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="callback.ready",
                            aggregate_type="callback_task",
                            aggregate_id=str(task_id),
                            task_name="app.tasks.deliver_callback",
                            queue="callback",
                            args=(task_id,),
                            dedup_key=f"callback:{task_id}:authority-busy:{lease_id}",
                        ),
                        available_delay_seconds=delay_s,
                    )
        finally:
            await engine.dispose()
        if not deferred:
            await self._fencing_miss(task_id, lease_id)

    async def mark_retry(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
        http_code: int | None,
        error: str | None,
    ) -> None:
        engine = self._engine()
        next_retry: object | None = None
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE callback_task SET
                          status='retrying',retry_count=retry_count+1,
                          next_retry_at=now()+make_interval(secs=>:delay_s),
                          lease_id=NULL,lease_expires_at=NULL,
                          last_http_code=:http_code,last_error=:error
                        WHERE id=:task_id AND status='retrying'
                          AND retry_count=:retry_count
                          AND lease_id=:lease_id AND lease_expires_at>now()
                        RETURNING retry_count
                        """
                    ),
                    {
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "retry_count": retry_count,
                        "delay_s": delay_s,
                        "http_code": http_code,
                        "error": _safe_error(error),
                    },
                )
                next_retry = result.scalar_one_or_none()
                if next_retry is not None:
                    assert isinstance(next_retry, int) and not isinstance(next_retry, bool)
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="callback.ready",
                            aggregate_type="callback_task",
                            aggregate_id=str(task_id),
                            task_name="app.tasks.deliver_callback",
                            queue="callback",
                            args=(task_id,),
                            dedup_key=f"callback:{task_id}:attempt:{int(next_retry)}",
                        ),
                        available_delay_seconds=delay_s,
                    )
        finally:
            await engine.dispose()
        if next_retry is None:
            await self._fencing_miss(task_id, lease_id)

    async def mark_dead(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        http_code: int | None,
        error: str | None,
    ) -> None:
        engine = self._engine()
        updated = False
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE callback_task SET
                          status='dead',finished_at=now(),next_retry_at=NULL,
                          lease_id=NULL,lease_expires_at=NULL,
                          last_http_code=:http_code,last_error=:error
                        WHERE id=:task_id AND status='retrying'
                          AND retry_count=:retry_count
                          AND lease_id=:lease_id AND lease_expires_at>now()
                        RETURNING id
                        """
                    ),
                    {
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "retry_count": retry_count,
                        "http_code": http_code,
                        "error": _safe_error(error),
                    },
                )
                updated = result.scalar_one_or_none() is not None
                if updated:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO worker_lease_event(
                              task_kind,task_id,event_type,lease_id
                            ) VALUES ('callback',:task_id,'dead',:lease_id)
                            """
                        ),
                        {"task_id": task_id, "lease_id": lease_id},
                    )
        finally:
            await engine.dispose()
        if not updated:
            await self._fencing_miss(task_id, lease_id)

    async def _state_update(self, statement: str, params: dict[str, object]) -> None:
        engine = self._engine()
        updated = False
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), params)
                updated = result.scalar_one_or_none() is not None
        finally:
            await engine.dispose()
        if not updated:
            task_id = params["task_id"]
            assert isinstance(task_id, int) and not isinstance(task_id, bool)
            lease_id = params["lease_id"]
            assert isinstance(lease_id, UUID)
            await self._fencing_miss(task_id, lease_id)

    async def _fencing_miss(self, task_id: int, lease_id: UUID) -> None:
        await self._record_lease_event(task_id, "fencing_miss", lease_id)
        raise CallbackLeaseLost("callback fencing token lost")

    async def _record_lease_event(
        self,
        task_id: int,
        event_type: str,
        lease_id: UUID,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO worker_lease_event(
                          task_kind,task_id,event_type,lease_id
                        ) VALUES ('callback',:task_id,:event_type,:lease_id)
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": event_type,
                        "lease_id": lease_id,
                    },
                )
        finally:
            await engine.dispose()

    async def load_material(
        self,
        task_id: int,
        lease_id: UUID,
    ) -> CallbackMaterial | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                task_result = await connection.execute(
                    text(
                        """
                        SELECT t.id,t.event_id,t.correlation_id,t.app_id,a.name app_name,
                          t.event,t.url,t.batch_id,
                          t.message_ids,t.message_times,t.event_keys,t.callback_secret_enc,
                          t.callback_secret_key_version,t.signature_version
                        FROM callback_task t JOIN app a ON a.id=t.app_id
                        WHERE t.id=:task_id AND t.status='retrying'
                          AND t.lease_id=:lease_id AND t.lease_expires_at>now()
                          AND a.status=1 AND a.callback_url=t.url
                          AND a.callback_secret_enc=t.callback_secret_enc
                          AND (
                            t.event<>'message.report'
                            OR a.callback_report_enabled=true
                          )
                        """
                    ),
                    {"task_id": task_id, "lease_id": lease_id},
                )
                row = task_result.mappings().one_or_none()
                if row is None or row["batch_id"] is None:
                    return None
                task = CallbackTaskRef(
                    task_id=int(row["id"]),
                    event_id=UUID(str(row["event_id"])),
                    correlation_id=UUID(str(row["correlation_id"])),
                    app_name=str(row["app_name"]),
                    event=str(row["event"]),
                    url=str(row["url"]),
                    callback_secret_enc=bytes(row["callback_secret_enc"]),
                    callback_secret_key_version=int(row["callback_secret_key_version"]),
                    signature_version=int(row["signature_version"]),
                    batch_id=int(row["batch_id"]),
                    message_ids=tuple(int(value) for value in row["message_ids"]),
                    message_times=tuple(row["message_times"]),
                )
                if task.event == "batch.finished":
                    batch_result = await connection.execute(
                        text(
                            """
                            SELECT trim(batch_no) batch_no,biz_id,category,status,total,
                              delivered,failed,unknown_cnt unknown,updated_at finished_at
                            FROM sms_batch WHERE id=:batch_id
                            """
                        ),
                        {"batch_id": task.batch_id},
                    )
                    batch_row = batch_result.mappings().one()
                    return CallbackMaterial(task, batch=BatchFinishedData(**dict(batch_row)))
                if task.event == "message.report":
                    messages_result = await connection.execute(
                        text(
                            """
                            WITH refs AS (
                              SELECT * FROM unnest(
                                CAST(:message_ids AS bigint[]),
                                CAST(:message_times AS timestamptz[]),
                                CAST(:event_keys AS char(64)[])
                              ) WITH ORDINALITY AS r(id,created_at,event_key,ord)
                            )
                            SELECT trim(b.batch_no) batch_no,b.biz_id,m.phone_enc,
                              trim(m.phone_hmac) phone_hmac,m.key_version,
                              e.message_status status,e.report_desc,
                              e.report_time
                            FROM refs r JOIN sms_message m
                              ON m.id=r.id AND m.created_at=r.created_at
                            JOIN callback_report_event e
                              ON e.event_key=r.event_key
                            JOIN sms_batch b ON b.id=m.batch_id
                            ORDER BY r.ord
                            """
                        ),
                        {
                            "message_ids": list(task.message_ids),
                            "message_times": list(task.message_times),
                            "event_keys": list(row["event_keys"]),
                        },
                    )
                    rows = list(messages_result.mappings())
                    if not rows:
                        raise ValueError("callback message references unavailable")
                    return CallbackMaterial(
                        task,
                        message_report=MessageReportData(
                            str(rows[0]["batch_no"]),
                            str(rows[0]["biz_id"]) if rows[0]["biz_id"] is not None else None,
                            tuple(
                                CallbackMessage(
                                    bytes(item["phone_enc"]),
                                    str(item["phone_hmac"]),
                                    int(item["key_version"]),
                                    str(item["status"]),
                                    str(item["report_desc"] or ""),
                                    item["report_time"],
                                )
                                for item in rows
                            ),
                        ),
                    )
                raise ValueError("unsupported callback event")
        finally:
            await engine.dispose()

    async def acquire_authority(
        self,
        task_id: int,
        lease_id: UUID,
    ) -> bool:
        """短事务内锁定 app 并登记最长 30 秒的在途权限租约。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                app_result = await connection.execute(
                    text(
                        """
                        SELECT a.id FROM callback_task t JOIN app a ON a.id=t.app_id
                        WHERE t.id=:task_id
                        FOR UPDATE OF a
                        """
                    ),
                    {"task_id": task_id},
                )
                app_id = app_result.scalar_one_or_none()
                if app_id is None:
                    return False
                await connection.execute(
                    text(
                        "DELETE FROM callback_authority_lease "
                        "WHERE app_id=:app_id AND expires_at<=now()"
                    ),
                    {"app_id": app_id},
                )
                inserted = await connection.execute(
                    text(
                        """
                        INSERT INTO callback_authority_lease(
                          app_id,task_id,lease_id,expires_at
                        )
                        SELECT t.app_id,t.id,:lease_id,now()+interval '30 seconds'
                        FROM callback_task t JOIN app a ON a.id=t.app_id
                        WHERE t.id=:task_id AND t.status='retrying'
                          AND t.lease_id=:lease_id AND t.lease_expires_at>now()
                          AND a.status=1 AND a.callback_url=t.url
                          AND a.callback_secret_enc=t.callback_secret_enc
                          AND (t.event<>'message.report' OR a.callback_report_enabled=true)
                        ON CONFLICT DO NOTHING RETURNING app_id
                        """
                    ),
                    {"task_id": task_id, "lease_id": lease_id},
                )
                if inserted.scalar_one_or_none() is not None:
                    return True
                busy = await connection.execute(
                    text(
                        "SELECT 1 FROM callback_authority_lease "
                        "WHERE app_id=:app_id AND expires_at>now()"
                    ),
                    {"app_id": app_id},
                )
                if busy.scalar_one_or_none() is not None:
                    raise CallbackAuthorityBusy("callback authority lease is busy")
                return False
        finally:
            await engine.dispose()

    async def release_authority(self, task_id: int, lease_id: UUID) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM callback_authority_lease "
                        "WHERE task_id=:task_id AND lease_id=:lease_id"
                    ),
                    {"task_id": task_id, "lease_id": lease_id},
                )
        finally:
            await engine.dispose()

    async def confirm_authority(self, task_id: int, lease_id: UUID) -> bool:
        """DNS 校验后再次串行化 app 变更，并把授权覆盖到有界 POST 完成。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                current = await connection.execute(
                    text(
                        """
                        SELECT a.id FROM callback_task t JOIN app a ON a.id=t.app_id
                        WHERE t.id=:task_id AND t.status='retrying'
                          AND t.lease_id=:lease_id AND t.lease_expires_at>now()
                          AND a.status=1 AND a.callback_url=t.url
                          AND a.callback_secret_enc=t.callback_secret_enc
                          AND (t.event<>'message.report' OR a.callback_report_enabled=true)
                        FOR UPDATE OF a
                        """
                    ),
                    {"task_id": task_id, "lease_id": lease_id},
                )
                app_id = current.scalar_one_or_none()
                if app_id is None:
                    return False
                renewed = await connection.execute(
                    text(
                        """
                        UPDATE callback_authority_lease
                        SET expires_at=now()+interval '30 seconds'
                        WHERE app_id=:app_id AND task_id=:task_id
                          AND lease_id=:lease_id AND expires_at>now()
                        RETURNING app_id
                        """
                    ),
                    {"app_id": app_id, "task_id": task_id, "lease_id": lease_id},
                )
                return renewed.scalar_one_or_none() is not None
        finally:
            await engine.dispose()
