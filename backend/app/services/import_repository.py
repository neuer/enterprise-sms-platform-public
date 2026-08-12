"""导入任务、手机号三列与无 PII 剔除文件持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.bounded_executor import run_bounded
from app.core.runtime_resources import (
    bind_connection_audit_subject,
    bind_connection_system_audit,
    database_engine,
)
from app.services.imports import ImportPhone, ImportResult
from app.settings import Settings, get_settings

_IMPORT_SUFFIXES = frozenset({".csv", ".xlsx"})


def canonical_import_filename(filename: str) -> str:
    """持久化仅保留解析所需的非敏感文件类型。"""

    suffix = Path(filename).suffix.casefold()
    if suffix not in _IMPORT_SUFFIXES:
        raise ValueError("unsupported import file type")
    return f"upload{suffix}"


class ImportStateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredImport:
    import_id: str
    valid: int
    invalid: int
    duplicate: int
    blacklisted: int
    invalid_file: str | None
    expires_at: datetime
    status: str = "ready"
    error: str | None = None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class ImportParseClaim:
    import_id: UUID
    lease_id: UUID
    filename: str
    source_file: str
    source_size: int


@dataclass(frozen=True, slots=True)
class ImportReservation:
    """导入包预留结果；consumed 重试只返回既有批次，不再读取号码。"""

    reservation_id: UUID
    expires_at: datetime
    phones: tuple[ImportPhone, ...] = ()
    consumed_batch_no: str | None = None


async def consume_import_reservation(
    connection: AsyncConnection,
    *,
    reservation_id: UUID,
    batch_id: int,
    principal: SecurityPrincipal,
) -> None:
    """在批次事务内把仍有效的预留原子固化到唯一批次。"""

    consumed = await connection.execute(
        text(
            """
            UPDATE import_task SET state='consumed',
              consumed_batch_id=:batch_id,consumed_at=now()
            WHERE reservation_id=CAST(:reservation_id AS uuid)
              AND state='reserved'
              AND reserved_by_account_id=:actor_account_id
              AND reservation_expires_at>now()
              AND consumed_batch_id IS NULL
            RETURNING id
            """
        ),
        {
            "reservation_id": str(reservation_id),
            "batch_id": batch_id,
            "actor_account_id": principal.account_id,
        },
    )
    if consumed.scalar_one_or_none() is None:
        raise ImportStateConflict("导入包预留已失效，请重试")


class SqlImportRepository:
    RESERVATION_LEASE_SECONDS = 300

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def persist(
        self,
        result: ImportResult,
        *,
        principal: SecurityPrincipal,
        filename: str,
        expire_hours: int,
    ) -> StoredImport:
        engine = self._engine()
        stored_path: Path | None = None
        try:
            async with engine.begin() as connection:
                inserted = await connection.execute(
                    text(
                        """
                        INSERT INTO import_task(
                          creator,creator_account_id,creator_identity_id,
                          filename,valid_cnt,invalid_cnt,dup_cnt,black_cnt,
                          parse_status,expires_at
                        ) VALUES(
                          :creator,:creator_account_id,:creator_identity_id,
                          :filename,:valid,:invalid,:duplicate,:blacklisted,
                          'ready',
                          now()+make_interval(hours=>:expire_hours))
                        RETURNING id,import_id,expires_at
                        """
                    ),
                    {
                        "creator": principal.login_name,
                        "creator_account_id": principal.account_id,
                        "creator_identity_id": principal.identity_id,
                        "filename": canonical_import_filename(filename),
                        "valid": len(result.valid),
                        "invalid": result.invalid,
                        "duplicate": result.duplicate,
                        "blacklisted": result.blacklisted,
                        "expire_hours": expire_hours,
                    },
                )
                task = inserted.mappings().one()
                if result.valid:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO import_phone(
                              import_task_id,phone_enc,phone_hmac,phone_mask,key_version,source_row
                            ) VALUES(:task_id,:phone_enc,:phone_hmac,:phone_mask,
                              :key_version,:source_row)
                            """
                        ),
                        [
                            {
                                "task_id": task["id"],
                                "phone_enc": item.phone_enc,
                                "phone_hmac": item.phone_hmac,
                                "phone_mask": item.phone_mask,
                                "key_version": item.key_version,
                                "source_row": item.source_row,
                            }
                            for item in result.valid
                        ],
                    )
                relative: str | None = None
                if result.removed:
                    await run_bounded(
                        self.settings.import_storage_dir.mkdir,
                        parents=True,
                        exist_ok=True,
                        timeout_s=5,
                    )
                    relative = f"{task['import_id']}.csv"
                    stored_path = self.settings.import_storage_dir / relative
                    await run_bounded(
                        stored_path.write_text,
                        result.removed_csv,
                        encoding="utf-8",
                        timeout_s=10,
                    )
                    await connection.execute(
                        text("UPDATE import_task SET invalid_file=:path WHERE id=:id"),
                        {"path": relative, "id": task["id"]},
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,'message_import','import_task',:object_id,
                          CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "role": principal.role,
                        "object_id": str(task["import_id"]),
                        "after": json.dumps(
                            {
                                "valid": len(result.valid),
                                "invalid": result.invalid,
                                "duplicate": result.duplicate,
                                "blacklisted": result.blacklisted,
                            }
                        ),
                    },
                )
                return StoredImport(
                    str(task["import_id"]),
                    len(result.valid),
                    result.invalid,
                    result.duplicate,
                    result.blacklisted,
                    relative,
                    task["expires_at"],
                )
        except Exception:
            if stored_path is not None:
                await run_bounded(
                    stored_path.unlink,
                    missing_ok=True,
                    timeout_s=5,
                )
            raise
        finally:
            await engine.dispose()

    async def register(
        self,
        *,
        principal: SecurityPrincipal,
        filename: str,
        source_size: int,
        expire_hours: int,
        ip: str,
    ) -> StoredImport:
        """登记待解析任务；不读取、不解析也不持久化号码明文。"""

        import_id = uuid4()
        source_file = f"import-{import_id}.smsx"
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO import_task(
                          import_id,
                          creator,creator_account_id,creator_identity_id,
                          filename,source_file,source_size,parse_status,expires_at
                        ) VALUES(
                          :import_id,
                          :creator,:account_id,:identity_id,:filename,:source_file,
                          :source_size,'staging',
                          now()+make_interval(hours=>:expire_hours)
                        )
                        RETURNING import_id,expires_at
                        """
                    ),
                    {
                        "import_id": import_id,
                        "creator": principal.login_name,
                        "account_id": principal.account_id,
                        "identity_id": principal.identity_id,
                        "filename": canonical_import_filename(filename),
                        "source_file": source_file,
                        "source_size": source_size,
                        "expire_hours": expire_hours,
                    },
                )
                row = result.mappings().one()
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        ip=ip,
                        action="message_import",
                        object_type="import_task",
                        object_id=str(row["import_id"]),
                        after={
                            "source_size": source_size,
                            "file_type": Path(filename).suffix.casefold(),
                            "parse_status": "staging",
                        },
                    ),
                )
                return StoredImport(
                    str(row["import_id"]),
                    0,
                    0,
                    0,
                    0,
                    None,
                    row["expires_at"],
                    status="staging",
                    source_file=source_file,
                )
        finally:
            await engine.dispose()

    async def attach_source(self, import_id: UUID, source_file: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                attached = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='pending'
                        WHERE import_id=:import_id AND parse_status='staging'
                          AND source_file=:source_file AND expires_at>now()
                        RETURNING id
                        """
                    ),
                    {"import_id": import_id, "source_file": source_file},
                )
                if attached.scalar_one_or_none() is None:
                    raise ImportStateConflict("导入任务登记状态已失效")
        finally:
            await engine.dispose()

    async def get_status(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> StoredImport | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT import_id,valid_cnt,invalid_cnt,dup_cnt,black_cnt,
                          invalid_file,expires_at,parse_status,parse_error
                        FROM import_task
                        WHERE import_id=CAST(:import_id AS uuid)
                          AND creator_account_id=:account_id
                          AND creator_identity_id IS NOT NULL
                          AND (expires_at>now() OR state='consumed')
                        """
                    ),
                    {
                        "import_id": import_id,
                        "account_id": principal.account_id,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                return StoredImport(
                    str(row["import_id"]),
                    int(row["valid_cnt"]),
                    int(row["invalid_cnt"]),
                    int(row["dup_cnt"]),
                    int(row["black_cnt"]),
                    (str(row["invalid_file"]) if row["invalid_file"] is not None else None),
                    row["expires_at"],
                    status=str(row["parse_status"]),
                    error=(str(row["parse_error"]) if row["parse_error"] is not None else None),
                )
        finally:
            await engine.dispose()

    async def fail_registration(self, import_id: UUID, error: str) -> None:
        """上传暂存失败时写入不含输入细节的稳定错误码。"""

        if error not in {"IMPORT_STAGE_FAILED", "IMPORT_QUEUE_UNAVAILABLE"}:
            raise ValueError("invalid import registration error")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='failed',parse_error=:error
                        WHERE import_id=:import_id
                          AND parse_status IN ('staging','pending')
                        """
                    ),
                    {"import_id": import_id, "error": error},
                )
        finally:
            await engine.dispose()

    async def pending_parse_ids(self, limit: int = 100) -> list[str]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='failed',
                          parse_error='IMPORT_PARSE_FAILED',
                          parse_lease_id=NULL,parse_lease_expires_at=NULL
                        WHERE parse_status='processing'
                          AND parse_lease_expires_at<=now()
                          AND parse_attempts>=3
                        """
                    )
                )
                result = await connection.execute(
                    text(
                        """
                        SELECT import_id::text FROM import_task
                        WHERE source_file IS NOT NULL AND expires_at>now()
                          AND parse_attempts<3
                          AND (
                            parse_status='pending'
                            OR (
                              parse_status='processing'
                              AND parse_lease_expires_at<=now()
                            )
                          )
                        ORDER BY created_at,id LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                return [str(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def claim_parse(self, import_id: str) -> ImportParseClaim | None:
        lease_id = uuid4()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                claimed = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='processing',
                          parse_lease_id=:lease_id,parse_started_at=now(),
                          parse_lease_expires_at=now()+interval '5 minutes',
                          parse_attempts=parse_attempts+1,parse_error=NULL,
                          valid_cnt=0,invalid_cnt=0,dup_cnt=0,black_cnt=0,
                          invalid_file=NULL
                        WHERE import_id=CAST(:import_id AS uuid)
                          AND source_file IS NOT NULL AND expires_at>now()
                          AND parse_attempts<3
                          AND (
                            parse_status='pending'
                            OR (
                              parse_status='processing'
                              AND parse_lease_expires_at<=now()
                            )
                          )
                        RETURNING import_id,filename,source_file,source_size
                        """
                    ),
                    {"import_id": import_id, "lease_id": str(lease_id)},
                )
                row = claimed.mappings().one_or_none()
                if row is None:
                    return None
                await connection.execute(
                    text(
                        """
                        DELETE FROM import_phone
                        WHERE import_task_id=(
                          SELECT id FROM import_task
                          WHERE import_id=CAST(:import_id AS uuid)
                            AND parse_lease_id=CAST(:lease_id AS uuid)
                        )
                        """
                    ),
                    {"import_id": import_id, "lease_id": str(lease_id)},
                )
                return ImportParseClaim(
                    UUID(str(row["import_id"])),
                    lease_id,
                    str(row["filename"]),
                    str(row["source_file"]),
                    int(row["source_size"]),
                )
        finally:
            await engine.dispose()

    async def append_parse_batch(
        self,
        claim: ImportParseClaim,
        phones: tuple[ImportPhone, ...],
    ) -> bool:
        """按 lease fencing 批量写受保护号码并续租。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                renewed = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET
                          parse_lease_expires_at=now()+interval '5 minutes'
                        WHERE import_id=:import_id
                          AND parse_status='processing'
                          AND parse_lease_id=:lease_id
                          AND parse_lease_expires_at>now()
                        RETURNING id
                        """
                    ),
                    {
                        "import_id": claim.import_id,
                        "lease_id": claim.lease_id,
                    },
                )
                task_id = renewed.scalar_one_or_none()
                if task_id is None:
                    return False
                if phones:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO import_phone(
                              import_task_id,phone_enc,phone_hmac,phone_mask,
                              key_version,source_row
                            ) VALUES(
                              :task_id,:phone_enc,:phone_hmac,:phone_mask,
                              :key_version,:source_row
                            )
                            ON CONFLICT(import_task_id,phone_hmac) DO NOTHING
                            """
                        ),
                        [
                            {
                                "task_id": task_id,
                                "phone_enc": item.phone_enc,
                                "phone_hmac": item.phone_hmac,
                                "phone_mask": item.phone_mask,
                                "key_version": item.key_version,
                                "source_row": item.source_row,
                            }
                            for item in phones
                        ],
                    )
                return True
        finally:
            await engine.dispose()

    async def finish_parse(
        self,
        claim: ImportParseClaim,
        *,
        valid: int,
        invalid: int,
        duplicate: int,
        blacklisted: int,
        invalid_file: str | None,
    ) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                finished = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='ready',
                          valid_cnt=:valid,invalid_cnt=:invalid,
                          dup_cnt=:duplicate,black_cnt=:blacklisted,
                          invalid_file=:invalid_file,parse_error=NULL,
                          parse_lease_id=NULL,parse_lease_expires_at=NULL
                        WHERE import_id=:import_id
                          AND parse_status='processing'
                          AND parse_lease_id=:lease_id
                          AND parse_lease_expires_at>now()
                        RETURNING id
                        """
                    ),
                    {
                        "import_id": claim.import_id,
                        "lease_id": claim.lease_id,
                        "valid": valid,
                        "invalid": invalid,
                        "duplicate": duplicate,
                        "blacklisted": blacklisted,
                        "invalid_file": invalid_file,
                    },
                )
                row = finished.mappings().one_or_none()
                if row is None:
                    return False
                await bind_connection_system_audit(
                    connection,
                    actor_name="import-parser",
                    action="message_import",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,
                          after_val
                        )
                        VALUES(
                          'import-parser','system','system',
                          'message_import','import_task',:object_id,
                          jsonb_build_object(
                            'valid',CAST(:valid AS integer),
                            'invalid',CAST(:invalid AS integer),
                            'duplicate',CAST(:duplicate AS integer),
                            'blacklisted',CAST(:blacklisted AS integer)
                          )
                        )
                        """
                    ),
                    {
                        "object_id": str(claim.import_id),
                        "valid": valid,
                        "invalid": invalid,
                        "duplicate": duplicate,
                        "blacklisted": blacklisted,
                    },
                )
                return True
        finally:
            await engine.dispose()

    async def release_parse(self, claim: ImportParseClaim) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                released = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET
                          parse_status=CASE
                            WHEN parse_attempts>=3 THEN 'failed'
                            ELSE 'pending'
                          END,
                          parse_lease_id=NULL,parse_lease_expires_at=NULL,
                          parse_error=CASE
                            WHEN parse_attempts>=3 THEN 'IMPORT_PARSE_FAILED'
                            ELSE 'IMPORT_RETRY_PENDING'
                          END
                        WHERE import_id=:import_id
                          AND parse_status='processing'
                          AND parse_lease_id=:lease_id
                        RETURNING id,parse_status
                        """
                    ),
                    {"import_id": claim.import_id, "lease_id": claim.lease_id},
                )
                row = released.mappings().one_or_none()
                if row is not None and row["parse_status"] == "failed":
                    await connection.execute(
                        text("DELETE FROM import_phone WHERE import_task_id=:task_id"),
                        {"task_id": int(row["id"])},
                    )
        finally:
            await engine.dispose()

    async def fail_parse(self, claim: ImportParseClaim, error: str) -> bool:
        if error not in {
            "IMPORT_FORMAT_INVALID",
            "IMPORT_TOO_LARGE",
            "IMPORT_PARSE_FAILED",
        }:
            raise ValueError("invalid import parse error")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                failed = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET parse_status='failed',
                          parse_error=:error,parse_lease_id=NULL,
                          parse_lease_expires_at=NULL
                        WHERE import_id=:import_id
                          AND parse_status='processing'
                          AND parse_lease_id=:lease_id
                        RETURNING id
                        """
                    ),
                    {
                        "import_id": claim.import_id,
                        "lease_id": claim.lease_id,
                        "error": error,
                    },
                )
                task_id = failed.scalar_one_or_none()
                if task_id is not None:
                    await connection.execute(
                        text("DELETE FROM import_phone WHERE import_task_id=:task_id"),
                        {"task_id": task_id},
                    )
                return task_id is not None
        finally:
            await engine.dispose()

    async def clear_source(self, claim: ImportParseClaim) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE import_task SET source_file=NULL
                        WHERE import_id=:import_id AND source_file=:source_file
                          AND parse_status IN ('ready','failed')
                        """
                    ),
                    {
                        "import_id": claim.import_id,
                        "source_file": claim.source_file,
                    },
                )
        finally:
            await engine.dispose()

    async def reserve(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> ImportReservation:
        """行锁串行预留；过期租约可抢占，已消费重试返回原批次。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                selected = await connection.execute(
                    text(
                        """
                        SELECT t.id,t.state,t.reservation_id,
                          t.reservation_expires_at,
                          trim(b.batch_no) consumed_batch_no
                        FROM import_task t
                        LEFT JOIN sms_batch b ON b.id=t.consumed_batch_id
                        WHERE t.import_id=CAST(:import_id AS uuid)
                          AND t.creator_account_id=:actor_account_id
                          AND t.creator_identity_id IS NOT NULL
                          AND t.parse_status='ready'
                          AND (t.expires_at>now() OR t.state='consumed')
                        FOR UPDATE OF t
                        """
                    ),
                    {
                        "import_id": import_id,
                        "actor_account_id": principal.account_id,
                    },
                )
                task = selected.mappings().one_or_none()
                if task is None:
                    raise ImportStateConflict("导入包不存在或已过期")
                if task["state"] == "consumed":
                    batch_no = task["consumed_batch_no"]
                    reservation_id = task["reservation_id"]
                    if batch_no is None or reservation_id is None:
                        raise ImportStateConflict("导入包历史状态不可重试")
                    return ImportReservation(
                        UUID(str(reservation_id)),
                        task["reservation_expires_at"],
                        consumed_batch_no=str(batch_no),
                    )
                reservation_id = uuid4()
                reserved = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET state='reserved',
                          reservation_id=:reservation_id,
                          reserved_by_account_id=:actor_account_id,
                          reserved_at=now(),
                          reservation_expires_at=
                            now()+make_interval(secs=>:lease_seconds)
                        WHERE id=:id AND (
                          state='ready'
                          OR (
                            state='reserved'
                            AND reservation_expires_at<=now()
                          )
                        )
                        RETURNING id,reservation_expires_at
                        """
                    ),
                    {
                        "id": task["id"],
                        "reservation_id": str(reservation_id),
                        "actor_account_id": principal.account_id,
                        "lease_seconds": self.RESERVATION_LEASE_SECONDS,
                    },
                )
                reserved_row = reserved.mappings().one_or_none()
                if reserved_row is None:
                    raise ImportStateConflict("导入包正在被其他请求使用")
                result = await connection.execute(
                    text(
                        """
                        SELECT phone_enc,phone_hmac,phone_mask,key_version,source_row
                        FROM import_phone WHERE import_task_id=:id ORDER BY source_row
                        """
                    ),
                    {"id": task["id"]},
                )
                return ImportReservation(
                    reservation_id,
                    reserved_row["reservation_expires_at"],
                    tuple(ImportPhone(**dict(row)) for row in result.mappings()),
                )
        finally:
            await engine.dispose()

    async def release(
        self,
        reservation_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> bool:
        """校验/持久化失败时释放本人仍持有的预留；consumed 永不回退。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                released = await connection.execute(
                    text(
                        """
                        UPDATE import_task SET state='ready',
                          reservation_id=NULL,reserved_by_account_id=NULL,
                          reserved_at=NULL,reservation_expires_at=NULL
                        WHERE reservation_id=CAST(:reservation_id AS uuid)
                          AND reserved_by_account_id=:actor_account_id
                          AND state='reserved' AND consumed_batch_id IS NULL
                        RETURNING id
                        """
                    ),
                    {
                        "reservation_id": str(reservation_id),
                        "actor_account_id": principal.account_id,
                    },
                )
                return released.scalar_one_or_none() is not None
        finally:
            await engine.dispose()

    async def invalid_file(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> Path | None:
        """仅向创建人暴露仍在有效期内的无 PII 剔除清单。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT invalid_file FROM import_task
                        WHERE import_id=:import_id
                          AND creator_account_id=:actor_account_id
                          AND creator_identity_id IS NOT NULL
                          AND expires_at>now() AND invalid_file IS NOT NULL
                        """
                    ),
                    {
                        "import_id": import_id,
                        "actor_account_id": principal.account_id,
                    },
                )
                relative = result.scalar_one_or_none()
        finally:
            await engine.dispose()
        if relative is None:
            return None
        root = self.settings.import_storage_dir.resolve()
        candidate = (root / str(relative)).resolve()
        if candidate.parent != root or not candidate.is_file():
            return None
        return candidate
