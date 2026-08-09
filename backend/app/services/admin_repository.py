"""审计与系统参数的 PostgreSQL 管理仓储。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import bind_connection_audit_subject, database_engine
from app.services.admin import (
    SENSITIVE_CONFIG_KEYS,
    AuditQuery,
    AuditRecord,
    ConfigRow,
    ConfigUpdate,
    InvalidAdminQuery,
)
from app.services.alert import validate_alert_destinations
from app.services.runtime_policy import InvalidRuntimePolicy, RuntimePolicy
from app.services.sensitive_config import (
    WECOM_WEBHOOK_KEY,
    AlertCredentialCipher,
    encrypt_wecom_webhook,
)
from app.settings import Settings, get_settings

CONFIG_SELECT = """
SELECT key,value,value_type,description,updated_by,updated_at
FROM sys_config WHERE LEFT(key,2) <> '__' ORDER BY key
"""
CONFIG_UPDATE_LOCK_ID = 7_260_202_607_13


def _config(row: Any) -> ConfigRow:
    key = str(row["key"])
    value = str(row["value"])
    return ConfigRow(
        key,
        value,
        str(row["value_type"]),
        str(row["description"]) if row["description"] is not None else None,
        str(row["updated_by"]) if row["updated_by"] is not None else None,
        row["updated_at"],
    )


class SqlAdminRepository:
    """系统配置更新和 config_update 审计在同一事务提交。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        credential_cipher: AlertCredentialCipher | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.credential_cipher = credential_cipher

    def _credential_cipher(self) -> AlertCredentialCipher:
        if self.credential_cipher is None:
            self.credential_cipher = AlertCredentialCipher.from_public_file(
                self.settings.alert_credential_public_key_file
            )
        return self.credential_cipher

    def _config(self, row: Any) -> ConfigRow:
        return _config(row)

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_configs(self) -> tuple[ConfigRow, ...]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(CONFIG_SELECT))
                return tuple(self._config(row) for row in result.mappings())
        finally:
            await engine.dispose()

    async def update_configs(
        self,
        updates: tuple[ConfigUpdate, ...],
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[ConfigRow, ...]:
        keys = [item.key for item in updates]
        if any(key.startswith("__") for key in keys):
            raise InvalidAdminQuery("配置项不存在或已被删除")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": CONFIG_UPDATE_LOCK_ID},
                )
                locked = await connection.execute(
                    text(
                        """
                        SELECT key,value,value_type,description,updated_by,updated_at
                        FROM sys_config
                        ORDER BY key FOR UPDATE
                        """
                    )
                )
                before = {str(row["key"]): self._config(row) for row in locked.mappings()}
                if any(key not in before for key in keys):
                    raise InvalidAdminQuery("配置项不存在或已被删除")
                effective = {key: row.value for key, row in before.items()}
                for item in updates:
                    if item.value is None:
                        raise InvalidAdminQuery(f"配置 {item.key} 缺少值")
                    effective[item.key] = item.value
                # 既有凭据只以密文存在，API 不持有解密能力。未轮换该项时，
                # 目标已在首次保存时校验，其他配置更新不得尝试解封。
                if WECOM_WEBHOOK_KEY not in keys:
                    effective[WECOM_WEBHOOK_KEY] = ""
                try:
                    RuntimePolicy.from_mapping(effective)
                    validate_alert_destinations(
                        effective,
                        self.settings.alert_smtp_allowed_host_set,
                    )
                except InvalidRuntimePolicy as error:
                    raise InvalidAdminQuery(str(error)) from error
                except ValueError as error:
                    raise InvalidAdminQuery(str(error)) from error
                stored_values = {
                    item.key: (
                        encrypt_wecom_webhook(
                            item.value or "", self._credential_cipher()
                        )
                        if item.key == WECOM_WEBHOOK_KEY
                        else item.value
                    )
                    for item in updates
                }
                await connection.execute(
                    text(
                        """
                        UPDATE sys_config SET value=:value,updated_by=:actor,updated_at=now()
                        WHERE key=:key
                        """
                    ),
                    [
                        {
                            "key": item.key,
                            "value": stored_values[item.key],
                            "actor": principal.login_name,
                        }
                        for item in updates
                    ],
                )
                for item in updates:
                    old_value = before[item.key].value
                    sensitive = item.key in SENSITIVE_CONFIG_KEYS
                    before_payload = (
                        {"configured": bool(old_value)} if sensitive else {"value": old_value}
                    )
                    after_payload = (
                        {"configured": bool(item.value)} if sensitive else {"value": item.value}
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,actor_subject_kind,actor_account_id,
                              actor_identity_id,role,ip,action,object_type,object_id,
                              before_val,after_val
                            ) VALUES(
                              :actor,'human',:actor_account_id,:actor_identity_id,
                              'admin',CAST(:ip AS inet),'config_update',
                              'sys_config',:key,CAST(:before AS jsonb),CAST(:after AS jsonb)
                            )
                            """
                        ),
                        {
                            "actor": principal.login_name,
                            "actor_account_id": principal.account_id,
                            "actor_identity_id": principal.identity_id,
                            "ip": ip,
                            "key": item.key,
                            "before": json.dumps(before_payload, ensure_ascii=False),
                            "after": json.dumps(after_payload, ensure_ascii=False),
                        },
                    )
                result = await connection.execute(text(CONFIG_SELECT))
                return tuple(self._config(row) for row in result.mappings())
        finally:
            await engine.dispose()

    async def list_audits(
        self,
        query: AuditQuery,
    ) -> tuple[tuple[AuditRecord, ...], int]:
        where = """
          (CAST(:actor AS varchar(64)) IS NULL OR actor=:actor)
          AND (CAST(:actor_account_id AS bigint) IS NULL
               OR actor_account_id=:actor_account_id)
          AND (CAST(:correlation_id AS uuid) IS NULL
               OR correlation_id=:correlation_id)
          AND (CAST(:action AS varchar(48)) IS NULL OR action=:action)
          AND (CAST(:object_type AS varchar(32)) IS NULL OR object_type=:object_type)
          AND (CAST(:object_id AS varchar(64)) IS NULL OR object_id=:object_id)
          AND (CAST(:start AS timestamptz) IS NULL OR created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR created_at<=:end)
        """
        params = {
            "actor": query.actor,
            "actor_account_id": query.actor_account_id,
            "correlation_id": query.correlation_id,
            "action": query.action,
            "object_type": query.object_type,
            "object_id": query.object_id,
            "start": query.start,
            "end": query.end,
            "limit": query.page_size,
            "offset": (query.page - 1) * query.page_size,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                total_result = await connection.execute(
                    text(f"SELECT count(*) FROM audit_log WHERE {where}"),
                    params,
                )
                result = await connection.execute(
                    text(
                        f"""
                        SELECT id,correlation_id,actor,actor_subject_kind,actor_account_id,
                          actor_identity_id,actor_app_id,role,host(ip) ip,
                          action,object_type,object_id,before_val,after_val,created_at
                        FROM audit_log WHERE {where}
                        ORDER BY created_at DESC,id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
                items = tuple(
                    AuditRecord(
                        int(row["id"]),
                        str(row["actor"]),
                        str(row["role"]) if row["role"] is not None else None,
                        str(row["ip"]) if row["ip"] is not None else None,
                        str(row["action"]),
                        str(row["object_type"]) if row["object_type"] is not None else None,
                        str(row["object_id"]) if row["object_id"] is not None else None,
                        row["before_val"],
                        row["after_val"],
                        row["created_at"],
                        str(row["actor_subject_kind"]),
                        (
                            int(row["actor_account_id"])
                            if row["actor_account_id"] is not None
                            else None
                        ),
                        (
                            int(row["actor_identity_id"])
                            if row["actor_identity_id"] is not None
                            else None
                        ),
                        (int(row["actor_app_id"]) if row["actor_app_id"] is not None else None),
                        UUID(str(row["correlation_id"])),
                    )
                    for row in result.mappings()
                )
                return items, int(total_result.scalar_one())
        finally:
            await engine.dispose()

    async def list_audit_actions(self) -> tuple[str, ...]:
        """审计动作去重清单；只读明细列，不触碰载荷。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT action FROM audit_log
                        GROUP BY action ORDER BY action LIMIT 500
                        """
                    )
                )
                return tuple(str(row["action"]) for row in result.mappings())
        finally:
            await engine.dispose()
