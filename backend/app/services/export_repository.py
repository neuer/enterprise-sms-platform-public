"""export_task PostgreSQL 事实源、租约与安全明细流。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.services.export import (
    MAX_EXPORT_ROWS,
    ExportFilterSet,
    ExportTaskInfo,
)
from app.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class ExportClaim:
    id: int
    filters: ExportFilterSet
    decrypted: bool
    lease_id: UUID
    lease_expires_at: datetime


class ExportLeaseLost(RuntimeError):
    """当前 export worker 的 fencing token 已失效。"""


@dataclass(frozen=True, slots=True)
class ExpiredExport:
    id: int
    file_path: str


def _message_where() -> str:
    return """
      (CAST(:scope_dept AS varchar(128)) IS NULL OR b.dept=:scope_dept)
      AND (CAST(:start AS timestamptz) IS NULL OR m.created_at>=:start)
      AND (CAST(:end AS timestamptz) IS NULL OR m.created_at<=:end)
      AND (CAST(:category AS varchar(8)) IS NULL OR b.category=:category)
      AND (CAST(:status AS varchar(10)) IS NULL OR m.status=:status)
      AND (CAST(:app_id AS bigint) IS NULL OR b.app_id=:app_id)
      AND (CAST(:batch_no AS char(32)) IS NULL OR b.batch_no=:batch_no)
      AND (:has_phone=false OR m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[])))
    """


def _unmatched_where() -> str:
    return """
      (CAST(:start AS timestamptz) IS NULL OR u.created_at>=:start)
      AND (CAST(:end AS timestamptz) IS NULL OR u.created_at<=:end)
      AND (:has_phone=false OR
        u.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[])))
    """


def _params(filters: ExportFilterSet) -> dict[str, object]:
    return {
        "scope_dept": filters.scope_dept,
        "start": filters.start,
        "end": filters.end,
        "category": filters.category,
        "status": filters.status,
        "app_id": filters.app_id,
        "batch_no": filters.batch_no,
        "has_phone": bool(filters.phone_hmacs),
        "phone_hmacs": list(filters.phone_hmacs),
    }


def _task(row: Any) -> ExportTaskInfo:
    return ExportTaskInfo(
        id=int(row["id"]),
        public_id=UUID(str(row["public_id"])),
        status=str(row["status"]),
        decrypted=bool(row["decrypted"]),
        row_count=int(row["row_count"]) if row["row_count"] is not None else None,
        file_path=str(row["file_path"]) if row["file_path"] is not None else None,
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


class SqlExportRepository:
    """过滤 SQL 固定白名单；只有 decrypted worker 查询会选择 phone_enc。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("export"))

    async def count_rows(self, filters: ExportFilterSet) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                if filters.dataset == "unmatched":
                    statement = (
                        "SELECT count(*) FROM (SELECT 1 FROM unmatched_report u WHERE "
                        + _unmatched_where()
                        + f" LIMIT {MAX_EXPORT_ROWS + 1}) bounded"
                    )
                else:
                    statement = (
                        "SELECT count(*) FROM (SELECT 1 FROM sms_message m "
                        "JOIN sms_batch b ON b.id=m.batch_id WHERE "
                        + _message_where()
                        + f" LIMIT {MAX_EXPORT_ROWS + 1}) bounded"
                    )
                result = await connection.execute(text(statement), _params(filters))
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    async def create(
        self,
        *,
        principal: SecurityPrincipal,
        filters: ExportFilterSet,
        decrypted: bool,
    ) -> ExportTaskInfo:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO export_task(
                          creator,creator_account_id,creator_identity_id,
                          scope_dept,scope_resolved,
                          filters,decrypted
                        )
                        VALUES(
                          :creator,:creator_account_id,:creator_identity_id,
                          :scope_dept,true,
                          CAST(:filters AS jsonb),:decrypted
                        )
                        RETURNING id,public_id,status,decrypted,row_count,file_path,
                          NULL::timestamptz expires_at,created_at
                        """
                    ),
                    {
                        "creator": principal.login_name,
                        "creator_account_id": principal.account_id,
                        "creator_identity_id": principal.identity_id,
                        "scope_dept": filters.scope_dept,
                        "filters": json.dumps(filters.safe_json(), ensure_ascii=False),
                        "decrypted": decrypted,
                    },
                )
                task = _task(result.mappings().one())
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,action,object_type,object_id,after_val
                        ) VALUES (
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :actor_role,'export_create','export_task',
                          CAST(:public_id AS text),
                          jsonb_build_object(
                            'actor_account_id',CAST(:actor_account_id AS bigint),
                            'actor_identity_id',CAST(:actor_identity_id AS bigint),
                            'decrypted',CAST(:decrypted AS boolean),'row_limit',100000,
                            'batch_no',CAST(:batch_no AS text),
                            'dataset',CAST(:dataset AS text),
                            'scope_dept',CAST(:scope_dept AS text)
                          )
                        )
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "actor_role": principal.role,
                        "public_id": str(task.public_id),
                        "decrypted": decrypted,
                        "batch_no": filters.batch_no,
                        "dataset": filters.dataset,
                        "scope_dept": filters.scope_dept,
                    },
                )
                return task
        finally:
            await engine.dispose()

    async def get_accessible(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        retention_days: int,
    ) -> ExportTaskInfo | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,public_id,status,decrypted,row_count,file_path,created_at,
                          CASE WHEN finished_at IS NULL THEN NULL
                            ELSE finished_at+make_interval(days=>:retention_days)
                          END expires_at
                        FROM export_task
                        WHERE public_id=:public_id
                          AND creator_account_id IS NOT NULL
                          AND creator_identity_id IS NOT NULL
                          AND scope_resolved
                          AND (
                            :actor_role='admin'
                            OR (
                              :actor_role='approver'
                              AND (
                                creator_account_id=:actor_account_id
                                OR (
                                  scope_dept IS NOT NULL
                                  AND scope_dept=CAST(:actor_dept AS varchar(128))
                                )
                              )
                            )
                            OR (
                              :actor_role IN ('operator','viewer')
                              AND creator_account_id=:actor_account_id
                            )
                          )
                          AND (
                            NOT decrypted
                            OR :actor_role IN ('admin','approver')
                          )
                        """
                    ),
                    {
                        "public_id": str(public_id),
                        "actor_account_id": principal.account_id,
                        "actor_role": principal.role,
                        "actor_dept": principal.dept,
                        "retention_days": retention_days,
                    },
                )
                row = result.mappings().one_or_none()
                return _task(row) if row is not None else None
        finally:
            await engine.dispose()

    async def get_downloadable_and_audit(
        self,
        public_id: UUID,
        *,
        principal: SecurityPrincipal,
        ip: str,
        retention_days: int,
    ) -> ExportTaskInfo | None:
        """原子执行下载授权和无 PII 审计；过期或未解析历史任务不留下载窗口。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,public_id,status,decrypted,row_count,file_path,created_at,
                          finished_at+make_interval(days=>:retention_days) expires_at,
                          scope_dept
                        FROM export_task
                        WHERE public_id=:public_id
                          AND creator_account_id IS NOT NULL
                          AND creator_identity_id IS NOT NULL
                          AND scope_resolved
                          AND status='done'
                          AND file_path IS NOT NULL
                          AND finished_at IS NOT NULL
                          AND finished_at+make_interval(days=>:retention_days)>now()
                          AND (
                            :actor_role='admin'
                            OR (
                              :actor_role='approver'
                              AND (
                                creator_account_id=:actor_account_id
                                OR (
                                  scope_dept IS NOT NULL
                                  AND scope_dept=CAST(:actor_dept AS varchar(128))
                                )
                              )
                            )
                            OR (
                              :actor_role IN ('operator','viewer')
                              AND creator_account_id=:actor_account_id
                            )
                          )
                          AND (
                            NOT decrypted
                            OR :actor_role IN ('admin','approver')
                          )
                        FOR SHARE
                        """
                    ),
                    {
                        "public_id": str(public_id),
                        "actor_account_id": principal.account_id,
                        "actor_role": principal.role,
                        "actor_dept": principal.dept,
                        "retention_days": retention_days,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,ip,action,object_type,object_id,after_val
                        ) VALUES (
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :actor_role,CAST(:ip AS inet),
                          'export_download','export_task',CAST(:public_id AS text),
                          jsonb_build_object(
                            'actor_account_id',CAST(:actor_account_id AS bigint),
                            'actor_identity_id',CAST(:actor_identity_id AS bigint),
                            'scope_dept',CAST(:scope_dept AS text),
                            'decrypted',CAST(:decrypted AS boolean),
                            'row_count',CAST(:row_count AS integer)
                          )
                        )
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "actor_role": principal.role,
                        "ip": ip,
                        "public_id": str(public_id),
                        "scope_dept": row["scope_dept"],
                        "decrypted": bool(row["decrypted"]),
                        "row_count": row["row_count"],
                    },
                )
                return _task(row)
        finally:
            await engine.dispose()

    async def pending_ids(self, limit: int = 100) -> list[int]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id FROM export_task
                        WHERE status='pending' OR (
                          status='running' AND lease_expires_at<=now()
                        ) ORDER BY COALESCE(lease_expires_at,created_at),id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                return [int(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int = 900,
    ) -> ExportClaim | None:
        if lease_seconds < 3:
            raise ValueError("export lease must be at least 3 seconds")
        lease_id = uuid4()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH candidate AS (
                          SELECT id,(lease_id IS NOT NULL) takeover
                          FROM export_task
                          WHERE id=:task_id AND (
                            status='pending' OR (
                              status='running' AND lease_expires_at<=now()
                            )
                          )
                          FOR UPDATE
                        ), claimed AS (
                          UPDATE export_task task SET
                            status='running',started_at=now(),
                            lease_id=:lease_id,
                            lease_expires_at=now()+make_interval(
                              secs=>:lease_seconds
                            ),
                            takeover_count=task.takeover_count+
                              CASE WHEN candidate.takeover THEN 1 ELSE 0 END
                          FROM candidate WHERE task.id=candidate.id
                          RETURNING task.id,task.filters,task.decrypted,
                            task.lease_id,task.lease_expires_at,candidate.takeover
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
                raw_filters = row["filters"]
                if not isinstance(raw_filters, dict):
                    raise ValueError("invalid persisted export filters")
                event_type = "takeover" if bool(row["takeover"]) else "acquired"
                await connection.execute(
                    text(
                        """
                        INSERT INTO worker_lease_event(
                          task_kind,task_id,event_type,lease_id
                        ) VALUES ('export',:task_id,:event_type,:lease_id)
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": event_type,
                        "lease_id": lease_id,
                    },
                )
                return ExportClaim(
                    int(row["id"]),
                    ExportFilterSet.from_safe_json(raw_filters),
                    bool(row["decrypted"]),
                    UUID(str(row["lease_id"])),
                    row["lease_expires_at"],
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
                        UPDATE export_task SET
                          lease_expires_at=now()+make_interval(
                            secs=>:lease_seconds
                          )
                        WHERE id=:task_id AND status='running'
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
        finally:
            await engine.dispose()
        if not renewed:
            await self._record_lease_event(task_id, "heartbeat_lost", lease_id)
        return renewed

    async def mark_done(
        self,
        task_id: int,
        *,
        lease_id: UUID,
        file_path: str,
        row_count: int,
    ) -> None:
        await self._lease_update(
            """
            UPDATE export_task SET status='done',file_path=:file_path,
              row_count=:row_count,finished_at=now(),
              lease_id=NULL,lease_expires_at=NULL
            WHERE id=:task_id AND status='running' AND lease_id=:lease_id
              AND lease_expires_at>now()
            RETURNING id
            """,
            {
                "task_id": task_id,
                "lease_id": lease_id,
                "file_path": file_path,
                "row_count": row_count,
            },
        )

    async def mark_failed(self, task_id: int, *, lease_id: UUID) -> None:
        await self._lease_update(
            """
            UPDATE export_task SET status='failed',file_path=NULL,finished_at=now(),
              lease_id=NULL,lease_expires_at=NULL
            WHERE id=:task_id AND status='running' AND lease_id=:lease_id
              AND lease_expires_at>now()
            RETURNING id
            """,
            {"task_id": task_id, "lease_id": lease_id},
        )

    async def _lease_update(
        self,
        statement: str,
        params: dict[str, object],
    ) -> None:
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
            await self._record_lease_event(task_id, "fencing_miss", lease_id)
            raise ExportLeaseLost("export fencing token lost")

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
                        ) VALUES ('export',:task_id,:event_type,:lease_id)
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

    async def _update(self, statement: str, params: dict[str, object]) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement), params)
        finally:
            await engine.dispose()

    async def retention_days(self) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT value FROM sys_config WHERE key='export_retention_days'")
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    async def expired(self, retention_days: int) -> list[ExpiredExport]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,file_path FROM export_task
                        WHERE file_path IS NOT NULL AND finished_at IS NOT NULL
                          AND finished_at+make_interval(days=>:days)<=now()
                        """
                    ),
                    {"days": retention_days},
                )
                return [
                    ExpiredExport(int(row["id"]), str(row["file_path"]))
                    for row in result.mappings()
                ]
        finally:
            await engine.dispose()

    async def clear_file(self, task_id: int, file_path: str) -> None:
        await self._update(
            "UPDATE export_task SET file_path=NULL WHERE id=:task_id AND file_path=:file_path",
            {"task_id": task_id, "file_path": file_path},
        )

    async def rows(self, claim: ExportClaim) -> AsyncIterator[dict[str, object]]:
        """按 task 固化 scope 顺序流出最多 100001 行；仅明文导出选择密文字段。"""

        if claim.filters.dataset == "unmatched":
            phone_columns = (
                "u.phone_enc,trim(u.phone_hmac) phone_hmac,u.key_version"
                if claim.decrypted
                else "u.phone_mask"
            )
            statement = text(
                f"""
                SELECT u.created_at,u.custom_id,u.vendor_task_id,{phone_columns},
                  u.report_status,u.report_desc,u.report_time
                FROM unmatched_report u WHERE {_unmatched_where()}
                ORDER BY u.created_at,u.id LIMIT {MAX_EXPORT_ROWS + 1}
                """
            )
        else:
            phone_columns = (
                "m.phone_enc,trim(m.phone_hmac) phone_hmac,m.key_version"
                if claim.decrypted
                else "m.phone_mask"
            )
            statement = text(
                f"""
                SELECT m.id,m.created_at,trim(b.batch_no) batch_no,b.category,
                  {phone_columns},m.status,m.report_desc,m.report_time,
                  b.display_content_enc
                FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                WHERE {_message_where()}
                ORDER BY m.created_at,m.id LIMIT {MAX_EXPORT_ROWS + 1}
                """
            )
        engine = self._engine()
        try:
            async with (
                engine.connect() as connection,
                connection.stream(statement, _params(claim.filters)) as result,
            ):
                async for row in result.mappings():
                    yield dict(row)
        finally:
            await engine.dispose()
