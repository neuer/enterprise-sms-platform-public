"""模板 PostgreSQL 事实源与状态约束。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import bind_connection_system_audit, database_engine
from app.services.template_management import TemplateRecord
from app.settings import Settings, get_settings

SELECT_FIELDS = """
SELECT id,name,content,COALESCE(var_specs,'[]'::jsonb) var_specs,dept,
  vendor_template_id,vendor_state,vendor_reject_reason
FROM sms_template
"""


def _record(row: Any) -> TemplateRecord:
    return TemplateRecord(
        int(row["id"]),
        str(row["name"]),
        str(row["content"]),
        list(row["var_specs"] or []),
        str(row["dept"]),
        str(row["vendor_template_id"]) if row["vendor_template_id"] is not None else None,
        str(row["vendor_state"]),
        str(row["vendor_reject_reason"]) if row["vendor_reject_reason"] is not None else None,
    )


class SqlTemplateRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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
                return [_record(row) for row in result.mappings()]
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
                return _record(row) if row is not None else None
        finally:
            await engine.dispose()

    async def create(self, **values: Any) -> TemplateRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO sms_template(
                          name,content,var_specs,dept,vendor_template_id,vendor_state,created_by
                        ) VALUES(:name,:content,CAST(:var_specs AS jsonb),:dept,
                          :vendor_template_id,'pending',:actor)
                        RETURNING id
                        """
                    ),
                    {**values, "var_specs": json.dumps(values["var_specs"])},
                )
                template_id = int(result.scalar_one())
                await self._audit(connection, values["actor"], "template_create", template_id)
                current = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE id=:id"), {"id": template_id}
                )
                return _record(current.mappings().one())
        finally:
            await engine.dispose()

    async def update(self, template_id: int, **values: Any) -> TemplateRecord | None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                changed = await connection.execute(
                    text(
                        """
                        UPDATE sms_template SET name=:name,content=:content,
                          var_specs=CAST(:var_specs AS jsonb),
                          vendor_template_id=:vendor_template_id,
                          vendor_state='pending',vendor_reject_reason=NULL,updated_at=now()
                        WHERE id=:id AND vendor_state IN ('draft','rejected') RETURNING id
                        """
                    ),
                    {"id": template_id, **values, "var_specs": json.dumps(values["var_specs"])},
                )
                if changed.scalar_one_or_none() is None:
                    return None
                await self._audit(connection, values["actor"], "template_update", template_id)
                current = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE id=:id"), {"id": template_id}
                )
                return _record(current.mappings().one())
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

    async def pending(self, template_id: int | None = None) -> list[TemplateRecord]:
        id_filter = "" if template_id is None else "AND id=:id"
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"{SELECT_FIELDS} WHERE vendor_state='pending' {id_filter}"),
                    {"id": template_id},
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
                for template_id, state, reason in states:
                    changed = await connection.execute(
                        text(
                            """
                            UPDATE sms_template SET vendor_state=:state,
                              vendor_reject_reason=:reason,updated_at=now()
                            WHERE id=:id AND vendor_state='pending'
                            RETURNING id
                            """
                        ),
                        {"id": template_id, "state": state, "reason": reason},
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
