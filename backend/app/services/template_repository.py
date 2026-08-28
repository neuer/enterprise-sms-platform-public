"""模板 PostgreSQL 事实源与状态约束。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import bind_connection_system_audit, database_engine
from app.core.sensitive_text import mask_phone_in_text
from app.services.content_protection import decrypt_template_content, decrypt_template_name
from app.services.crypto import CryptoService, EncryptionContext
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.template_management import TemplateRecord, TemplateStateUpdate
from app.settings import Settings, get_settings

SELECT_FIELDS = """
SELECT id,name_enc,content_enc,COALESCE(var_specs,'[]'::jsonb) var_specs,dept,
  vendor_template_id,vendor_state,vendor_reject_reason
FROM sms_template
"""


def _record(row: Any, crypto: CryptoService) -> TemplateRecord:
    template_id = int(row["id"])
    return TemplateRecord(
        template_id,
        decrypt_template_name(crypto, row["name_enc"], template_id),
        decrypt_template_content(crypto, row["content_enc"], template_id),
        list(row["var_specs"] or []),
        str(row["dept"]),
        str(row["vendor_template_id"]) if row["vendor_template_id"] is not None else None,
        str(row["vendor_state"]),
        str(row["vendor_reject_reason"]) if row["vendor_reject_reason"] is not None else None,
    )


class SqlTemplateRepository:
    def __init__(
        self,
        settings: Settings | None = None,
        crypto: CryptoService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.crypto = crypto

    def _crypto(self) -> CryptoService:
        if self.crypto is None:
            self.crypto = CryptoService.from_settings(self.settings)
        return self.crypto

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        where = "" if dept is None else "WHERE dept=:dept"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"{SELECT_FIELDS} {where} ORDER BY updated_at DESC"), {"dept": dept}
                )
                return [_record(row, self._crypto()) for row in result.mappings()]
        finally:
            await engine.dispose()

    async def get(self, template_id: int, *, dept: str | None = None) -> TemplateRecord | None:
        dept_sql = "" if dept is None else "AND dept=:dept"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE id=:id {dept_sql}"),
                    {"id": template_id, "dept": dept},
                )
                row = result.mappings().one_or_none()
                return _record(row, self._crypto()) if row is not None else None
        finally:
            await engine.dispose()

    async def create(
        self,
        *,
        name: str,
        content: str,
        var_specs: list[dict[str, int]],
        dept: str,
        actor: str,
    ) -> TemplateRecord:
        values: dict[str, Any] = {
            "var_specs": var_specs,
            "dept": dept,
            "actor": actor,
        }
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                template_id = int(
                    await connection.scalar(text("SELECT nextval('sms_template_id_seq')"))
                )
                content_enc = self._crypto().encrypt_bound_packed_text(
                    content,
                    EncryptionContext(
                        domain="sms-template-content",
                        table="sms_template",
                        column="content_enc",
                        object_id=str(template_id),
                    ),
                )
                name_enc = self._crypto().encrypt_bound_packed_text(
                    name,
                    EncryptionContext(
                        domain="sms-template-name",
                        table="sms_template",
                        column="name_enc",
                        object_id=str(template_id),
                    ),
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_template(
                          id,name,name_enc,content,content_enc,var_specs,dept,
                          vendor_template_id,vendor_state,created_by
                        ) VALUES(:id,'[encrypted]',:name_enc,'[encrypted]',:content_enc,
                          CAST(:var_specs AS jsonb),:dept,
                          NULL,'pending',:actor)
                        """
                    ),
                    {
                        "id": template_id,
                        "name_enc": name_enc,
                        "content_enc": content_enc,
                        **values,
                        "var_specs": json.dumps(values["var_specs"]),
                    },
                )
                await self._audit(connection, values["actor"], "template_create", template_id)
                await self._enqueue_binding(connection, template_id)
                current = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE id=:id"), {"id": template_id}
                )
                return _record(current.mappings().one(), self._crypto())
        finally:
            await engine.dispose()

    async def update(
        self,
        template_id: int,
        *,
        name: str,
        content: str,
        var_specs: list[dict[str, int]],
        actor: str,
    ) -> TemplateRecord | None:
        values: dict[str, Any] = {
            "name_enc": self._crypto().encrypt_bound_packed_text(
                name,
                EncryptionContext(
                    domain="sms-template-name",
                    table="sms_template",
                    column="name_enc",
                    object_id=str(template_id),
                ),
            ),
            "content_enc": self._crypto().encrypt_bound_packed_text(
                content,
                EncryptionContext(
                    domain="sms-template-content",
                    table="sms_template",
                    column="content_enc",
                    object_id=str(template_id),
                ),
            ),
            "var_specs": var_specs,
            "actor": actor,
        }
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_template SET name='[encrypted]',name_enc=:name_enc,
                          content='[encrypted]',
                          content_enc=:content_enc,
                          var_specs=CAST(:var_specs AS jsonb),
                          vendor_template_id=NULL,
                          vendor_state='pending',vendor_reject_reason=NULL,updated_at=now()
                        WHERE id=:id AND vendor_state IN ('draft','rejected') RETURNING id
                        """
                    ),
                    {"id": template_id, **values, "var_specs": json.dumps(values["var_specs"])},
                )
                if changed.scalar_one_or_none() is None:
                    return None
                await self._audit(connection, values["actor"], "template_update", template_id)
                await self._enqueue_binding(connection, template_id)
                current = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE id=:id"), {"id": template_id}
                )
                return _record(current.mappings().one(), self._crypto())
        finally:
            await engine.dispose()

    async def delete(self, template_id: int, *, actor: str) -> bool:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        DELETE FROM sms_template t WHERE t.id=:id
                          AND t.vendor_state IN ('draft','pending','rejected')
                          AND NOT EXISTS(SELECT 1 FROM sms_batch b WHERE b.template_id=t.id)
                        RETURNING id
                        """
                    ),
                    {"id": template_id},
                )
                removed = result.scalar_one_or_none() is not None
                if removed:
                    await self._audit(connection, actor, "template_delete", template_id)
                return removed
        finally:
            await engine.dispose()

    async def syncable(self, template_id: int | None = None) -> list[TemplateRecord]:
        """返回仍需跟踪厂商审核结果的模板。"""

        id_filter = "" if template_id is None else "AND id=:id"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"{SELECT_FIELDS} WHERE vendor_state IN ('pending','rejected') "
                        f"AND vendor_template_id IS NOT NULL {id_filter}"
                    ),
                    {"id": template_id},
                )
                return [_record(row, self._crypto()) for row in result.mappings()]
        finally:
            await engine.dispose()

    async def apply_states(self, states: list[TemplateStateUpdate]) -> int:
        if not states:
            return 0
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                applied = 0
                for template_id, expected_vendor_template_id, state, reason in states:
                    reason = mask_phone_in_text(reason)
                    changed = await connection.execute(
                        text(
                            """
                            UPDATE sms_template SET vendor_state=:state,
                              vendor_reject_reason=:reason,updated_at=now()
                            WHERE id=:id AND vendor_state IN ('pending','rejected')
                              AND vendor_template_id=:expected_vendor_template_id
                              AND (
                                vendor_state IS DISTINCT FROM :state
                                OR vendor_reject_reason IS DISTINCT FROM :reason
                              )
                            RETURNING id
                            """
                        ),
                        {
                            "id": template_id,
                            "expected_vendor_template_id": expected_vendor_template_id,
                            "state": state,
                            "reason": reason,
                        },
                    )
                    if changed.scalar_one_or_none() is None:
                        continue
                    principal = current_audit_principal()
                    if principal is None:
                        await bind_connection_system_audit(
                            connection,
                            actor_name="vendor-state-sync",
                            action="template_sync",
                        )
                        await connection.execute(
                            text(
                                """
                                INSERT INTO audit_log(
                                  actor,actor_subject_kind,role,action,object_type,
                                  object_id,after_val
                                ) VALUES(
                                  'vendor-state-sync','system','system','template_sync',
                                  'template',CAST(CAST(:id AS bigint) AS text),
                                  jsonb_build_object('vendor_state',CAST(:state AS text))
                                )
                                """
                            ),
                            {"id": template_id, "state": state},
                        )
                    else:
                        await insert_audit(
                            connection,
                            AuditEvent(
                                principal=principal,
                                action="template_sync",
                                object_type="template",
                                object_id=str(template_id),
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

    async def apply_binding(self, template_id: int, vendor_template_id: str) -> bool:
        """仅在仍待提交且尚无厂商 ID 时落绑定结果，并记录系统审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_template SET vendor_template_id=:vendor_template_id,
                          updated_at=now()
                        WHERE id=:id AND vendor_state='pending'
                          AND vendor_template_id IS NULL
                        RETURNING id
                        """
                    ),
                    {"id": template_id, "vendor_template_id": vendor_template_id},
                )
                if changed.scalar_one_or_none() is None:
                    return False
                await bind_connection_system_audit(
                    connection,
                    actor_name="vendor-state-sync",
                    action="template_sync",
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,
                          object_id,after_val
                        ) VALUES(
                          'vendor-state-sync','system','system','template_sync',
                          'template',CAST(CAST(:id AS bigint) AS text),
                          jsonb_build_object('vendor_binding','submitted')
                        )
                        """
                    ),
                    {"id": template_id},
                )
                return True
        finally:
            await engine.dispose()

    @staticmethod
    async def _enqueue_binding(connection: Any, template_id: int) -> None:
        request_id = uuid4()
        await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="template.bind",
                aggregate_type="sms_template",
                aggregate_id=str(template_id),
                task_name="app.tasks.bind_template",
                queue="realtime",
                args=(template_id,),
                dedup_key=f"template.bind:{template_id}:{request_id}",
                max_attempts=1,
            ),
        )

    @staticmethod
    async def _audit(connection: Any, actor: str, action: str, template_id: int) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(actor,action,object_type,object_id,after_val)
                VALUES(:actor,:action,'template',CAST(CAST(:id AS bigint) AS text),
                  jsonb_build_object('template_id',:id))
                """
            ),
            {"actor": actor, "action": action, "id": template_id},
        )
