"""签名 PostgreSQL 事实源与发送审核查询。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import bind_connection_system_audit, database_engine
from app.core.sensitive_text import mask_phone_in_text
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.sign import format_sign_name
from app.services.sign_management import SignRecord, SignStateConflict
from app.settings import Settings, get_settings

FIELDS = "id,name,vendor_sign_id,vendor_state,vendor_reject_reason"


def _record(row: Any) -> SignRecord:
    return SignRecord(
        int(row["id"]),
        str(row["name"]),
        str(row["vendor_sign_id"]) if row["vendor_sign_id"] is not None else None,
        str(row["vendor_state"]),
        str(row["vendor_reject_reason"]) if row["vendor_reject_reason"] is not None else None,
    )


class SqlSignRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_all(self) -> list[SignRecord]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT {FIELDS} FROM sms_sign ORDER BY created_at DESC")
                )
                return [_record(row) for row in result.mappings()]
        finally:
            await engine.dispose()

    async def get(self, sign_id: int) -> SignRecord | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT {FIELDS} FROM sms_sign WHERE id=:id"), {"id": sign_id}
                )
                row = result.mappings().one_or_none()
                return _record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def is_approved(self, plain_name: str) -> bool:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM sms_sign "
                        "WHERE name=:name AND vendor_state='approved')"
                    ),
                    {"name": plain_name},
                )
                return bool(result.scalar_one())
        finally:
            await engine.dispose()

    async def create(self, *, name: str, actor: str) -> SignRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO sms_sign(name,vendor_sign_id,vendor_state,created_by)
                        VALUES(:name,NULL,'pending',:actor) RETURNING id
                        """
                    ),
                    {"name": name, "actor": actor},
                )
                sign_id = int(result.scalar_one())
                await self._audit(connection, actor, "sign_create", sign_id)
                await self._enqueue_binding(connection, sign_id)
                current = await connection.execute(
                    text(f"SELECT {FIELDS} FROM sms_sign WHERE id=:id"), {"id": sign_id}
                )
                return _record(current.mappings().one())
        finally:
            await engine.dispose()

    async def update(
        self, sign_id: int, *, name: str, actor: str
    ) -> SignRecord | None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_sign SET name=:name,vendor_sign_id=NULL,
                          vendor_state='pending',vendor_reject_reason=NULL
                        WHERE id=:id AND vendor_state='rejected' RETURNING id
                        """
                    ),
                    {"id": sign_id, "name": name},
                )
                if changed.scalar_one_or_none() is None:
                    return None
                await self._audit(connection, actor, "sign_update", sign_id)
                await self._enqueue_binding(connection, sign_id)
                current = await connection.execute(
                    text(f"SELECT {FIELDS} FROM sms_sign WHERE id=:id"), {"id": sign_id}
                )
                return _record(current.mappings().one())
        finally:
            await engine.dispose()

    async def delete(self, sign_id: int, *, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                record = await connection.execute(
                    text("SELECT name FROM sms_sign WHERE id=:id"), {"id": sign_id}
                )
                name = record.scalar_one_or_none()
                if name is None:
                    return False
                formatted = format_sign_name(str(name))
                deleted = await connection.execute(
                    text(
                        """
                        DELETE FROM sms_sign s WHERE s.id=:id AND s.vendor_state<>'approved'
                          AND NOT EXISTS(SELECT 1 FROM app a WHERE a.default_sign=s.name)
                          AND NOT EXISTS(SELECT 1 FROM sms_batch b
                            WHERE b.sign_name IN (s.name,:formatted)) RETURNING id
                        """
                    ),
                    {"id": sign_id, "formatted": formatted},
                )
                removed = deleted.scalar_one_or_none() is not None
                if removed:
                    await self._audit(connection, actor, "sign_delete", sign_id)
                return removed
        finally:
            await engine.dispose()

    async def pending(self, sign_id: int | None = None) -> list[SignRecord]:
        id_filter = "" if sign_id is None else "AND id=:id"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT {FIELDS} FROM sms_sign WHERE vendor_state='pending' {id_filter}"),
                    {"id": sign_id},
                )
                return [_record(row) for row in result.mappings()]
        finally:
            await engine.dispose()

    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int:
        if not states:
            return 0
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                applied = 0
                for sign_id, state, reason in states:
                    reason = mask_phone_in_text(reason)
                    changed = await connection.execute(
                        text(
                            """
                            UPDATE sms_sign SET vendor_state=:state,vendor_reject_reason=:reason
                            WHERE id=:id AND vendor_state='pending'
                            RETURNING id
                            """
                        ),
                        {"id": sign_id, "state": state, "reason": reason},
                    )
                    if changed.scalar_one_or_none() is None:
                        continue
                    principal = current_audit_principal()
                    if principal is None:
                        await bind_connection_system_audit(
                            connection,
                            actor_name="vendor-state-sync",
                            action="sign_sync",
                        )
                        await connection.execute(
                            text(
                                """
                                INSERT INTO audit_log(
                                  actor,actor_subject_kind,role,action,object_type,
                                  object_id,after_val
                                ) VALUES(
                                  'vendor-state-sync','system','system','sign_sync',
                                  'sign',CAST(CAST(:id AS bigint) AS text),
                                  jsonb_build_object('vendor_state',CAST(:state AS text))
                                )
                                """
                            ),
                            {"id": sign_id, "state": state},
                        )
                    else:
                        await insert_audit(
                            connection,
                            AuditEvent(
                                principal=principal,
                                action="sign_sync",
                                object_type="sign",
                                object_id=str(sign_id),
                                role=(
                                    principal.role
                                    if hasattr(principal, "role")
                                    else None
                                ),
                                after={"vendor_state": state},
                            ),
                        )
                    applied += 1
                return applied
        finally:
            await engine.dispose()

    async def apply_binding(self, sign_id: int, vendor_sign_id: str) -> bool:
        """仅在仍待提交且尚无厂商 ID 时落绑定结果，并记录系统审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_sign SET vendor_sign_id=:vendor_sign_id
                        WHERE id=:id AND vendor_state='pending'
                          AND vendor_sign_id IS NULL
                        RETURNING id
                        """
                    ),
                    {"id": sign_id, "vendor_sign_id": vendor_sign_id},
                )
                if changed.scalar_one_or_none() is None:
                    return False
                await bind_connection_system_audit(
                    connection,
                    actor_name="vendor-state-sync",
                    action="sign_sync",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,
                          object_id,after_val
                        ) VALUES(
                          'vendor-state-sync','system','system','sign_sync',
                          'sign',CAST(CAST(:id AS bigint) AS text),
                          jsonb_build_object('vendor_binding','submitted')
                        )
                        """
                    ),
                    {"id": sign_id},
                )
                return True
        finally:
            await engine.dispose()

    async def adopt_existing(
        self,
        sign_id: int,
        vendor_sign_id: str,
        vendor_state: str,
        vendor_reject_reason: str | None,
    ) -> bool:
        """原子采用已核验厂商 ID 与真实审核状态，并写系统审计。"""

        if vendor_state not in {"pending", "approved", "rejected"}:
            raise ValueError("invalid vendor sign state")
        reason = mask_phone_in_text(vendor_reject_reason)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await self._lock_vendor_binding(connection, sign_id, vendor_sign_id)
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_sign
                        SET vendor_sign_id=:vendor_sign_id,
                            vendor_state=:vendor_state,
                            vendor_reject_reason=:vendor_reject_reason
                        WHERE id=:id AND vendor_state='pending'
                          AND vendor_sign_id IS NULL
                        RETURNING id
                        """
                    ),
                    {
                        "id": sign_id,
                        "vendor_sign_id": vendor_sign_id,
                        "vendor_state": vendor_state,
                        "vendor_reject_reason": reason,
                    },
                )
                if changed.scalar_one_or_none() is None:
                    return False
                await bind_connection_system_audit(
                    connection,
                    actor_name="vendor-state-sync",
                    action="sign_adopt",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,
                          object_id,after_val
                        ) VALUES(
                          'vendor-state-sync','system','system','sign_adopt',
                          'sign',CAST(CAST(:id AS bigint) AS text),
                          jsonb_build_object(
                            'vendor_binding','adopted',
                            'vendor_state',CAST(:vendor_state AS text),
                            'vendor_sign_id',CAST(:vendor_sign_id AS text)
                          )
                        )
                        """
                    ),
                    {
                        "id": sign_id,
                        "vendor_state": vendor_state,
                        "vendor_sign_id": vendor_sign_id,
                    },
                )
                return True
        finally:
            await engine.dispose()

    @staticmethod
    async def _lock_vendor_binding(
        connection: Any,
        sign_id: int,
        vendor_sign_id: str,
    ) -> None:
        """按厂商 ID 串行绑定，并拒绝跨本地签名重复关联。"""

        await connection.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('sms_sign:' || CAST(:vendor_sign_id AS text),0))"
            ),
            {"vendor_sign_id": vendor_sign_id},
        )
        duplicate = await connection.execute(
            text(
                """
                SELECT id FROM sms_sign
                WHERE vendor_sign_id=:vendor_sign_id AND id<>:id
                LIMIT 1
                """
            ),
            {"id": sign_id, "vendor_sign_id": vendor_sign_id},
        )
        if duplicate.scalar_one_or_none() is not None:
            raise SignStateConflict("厂商签名编号已关联其他本地签名")

    @staticmethod
    async def _enqueue_binding(connection: Any, sign_id: int) -> None:
        request_id = uuid4()
        await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="sign.bind",
                aggregate_type="sms_sign",
                aggregate_id=str(sign_id),
                task_name="app.tasks.bind_sign",
                queue="realtime",
                args=(sign_id,),
                dedup_key=f"sign.bind:{sign_id}:{request_id}",
                max_attempts=1,
            ),
        )

    @staticmethod
    async def _audit(connection: Any, actor: str, action: str, sign_id: int) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                VALUES(:actor,:action,'sign',CAST(CAST(:id AS bigint) AS text),
                  jsonb_build_object('sign_id',:id))
                """
            ),
            {"actor": actor, "action": action, "id": sign_id},
        )
