"""认证源版本状态与安全审计的 PostgreSQL 原子仓储。"""

from __future__ import annotations

import json
from typing import Any, cast

from sqlalchemy import text

from app.core.auth.roles import Role
from app.core.runtime_resources import database_engine
from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant
from app.services.auth_provider import (
    ExternalRoleMapping,
    ProviderNotFound,
    ProviderRecord,
    ProviderTestResult,
    StaleProviderDraft,
    UntestedProviderConfig,
)
from app.settings import Settings, get_settings

PROVIDER_COLUMNS = """
id,code,name,kind,enabled,draft_config,active_config,draft_version,
tested_version,active_version,last_tested_at,last_test_status,created_at,updated_at
"""


def _provider(row: Any) -> ProviderRecord:
    return ProviderRecord(
        id=int(row["id"]),
        code=str(row["code"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        enabled=bool(row["enabled"]),
        draft_config=dict(row["draft_config"]),
        active_config=(dict(row["active_config"]) if row["active_config"] is not None else None),
        draft_version=int(row["draft_version"]),
        tested_version=(int(row["tested_version"]) if row["tested_version"] is not None else None),
        active_version=(int(row["active_version"]) if row["active_version"] is not None else None),
        last_tested_at=row["last_tested_at"],
        last_test_status=(
            str(row["last_test_status"]) if row["last_test_status"] is not None else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _one_or_none(result: Any) -> Any:
    return result.mappings().one_or_none()


class SqlAuthProviderRepository:
    """每次认证源变更与其无敏感审计在同一事务提交。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("auth"))

    async def list_enabled(self) -> tuple[ProviderRecord, ...]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT {PROVIDER_COLUMNS}
                        FROM auth_provider
                        WHERE enabled=TRUE
                        ORDER BY CASE WHEN code='local' THEN 0 ELSE 1 END,name,code
                        """
                    )
                )
                return tuple(_provider(row) for row in result.mappings())
        finally:
            await engine.dispose()

    async def get(self, code: str) -> ProviderRecord:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT {PROVIDER_COLUMNS} FROM auth_provider WHERE code=:code"),
                    {"code": code},
                )
                row = _one_or_none(result)
                if row is None:
                    raise ProviderNotFound("认证源不存在")
                return _provider(row)
        finally:
            await engine.dispose()

    async def save_draft(
        self,
        code: str,
        config: dict[str, object],
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                locked = await connection.execute(
                    text(
                        f"""
                        SELECT {PROVIDER_COLUMNS}
                        FROM auth_provider WHERE code=:code FOR UPDATE
                        """
                    ),
                    {"code": code},
                )
                if _one_or_none(locked) is None:
                    raise ProviderNotFound("认证源不存在")
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE auth_provider
                        SET draft_config=CAST(:config AS jsonb),
                            draft_version=draft_version+1,
                            tested_version=NULL,
                            last_tested_at=NULL,
                            last_test_status=NULL,
                            updated_at=now()
                        WHERE code=:code
                        RETURNING {PROVIDER_COLUMNS}
                        """
                    ),
                    {
                        "code": code,
                        "config": json.dumps(config, ensure_ascii=False),
                    },
                )
                row = _one_or_none(result)
                if row is None:
                    raise ProviderNotFound("认证源不存在")
                saved = _provider(row)
                await self._audit(
                    connection,
                    code=code,
                    version=saved.draft_version,
                    action="save_draft",
                    result_code="SAVED",
                    actor=actor,
                    ip=ip,
                )
                return saved
        finally:
            await engine.dispose()

    async def record_test(
        self,
        code: str,
        version: int,
        result: ProviderTestResult,
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                updated = await connection.execute(
                    text(
                        f"""
                        UPDATE auth_provider
                        SET tested_version=CASE WHEN :success THEN :version ELSE NULL END,
                            last_tested_at=now(),
                            last_test_status=CASE WHEN :success THEN 'success' ELSE 'failed' END,
                            updated_at=now()
                        WHERE code=:code AND draft_version=:version
                        RETURNING {PROVIDER_COLUMNS}
                        """
                    ),
                    {
                        "code": code,
                        "version": version,
                        "success": result.success,
                    },
                )
                row = _one_or_none(updated)
                if row is None:
                    raise StaleProviderDraft("测试期间认证源草稿已改变，请重新测试")
                saved = _provider(row)
                await self._audit(
                    connection,
                    code=code,
                    version=version,
                    action="test",
                    result_code=result.result_code,
                    actor=actor,
                    ip=ip,
                )
                return saved
        finally:
            await engine.dispose()

    async def activate(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE auth_provider
                        SET active_config=draft_config,
                            active_version=draft_version,
                            enabled=TRUE,
                            updated_at=now()
                        WHERE code=:code AND tested_version=draft_version
                        RETURNING {PROVIDER_COLUMNS}
                        """
                    ),
                    {"code": code},
                )
                row = _one_or_none(result)
                if row is None:
                    raise UntestedProviderConfig("当前认证源草稿尚未通过测试")
                saved = _provider(row)
                await self._audit(
                    connection,
                    code=code,
                    version=saved.active_version or saved.draft_version,
                    action="activate",
                    result_code="ACTIVATED",
                    actor=actor,
                    ip=ip,
                )
                return saved
        finally:
            await engine.dispose()

    async def disable(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_admin_invariant(connection)
                locked = await connection.execute(
                    text(
                        f"""
                        SELECT {PROVIDER_COLUMNS}
                        FROM auth_provider WHERE code=:code FOR UPDATE
                        """
                    ),
                    {"code": code},
                )
                previous = _one_or_none(locked)
                if previous is None:
                    raise ProviderNotFound("认证源不存在")
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE auth_provider SET enabled=FALSE,updated_at=now()
                        WHERE code=:code
                        RETURNING {PROVIDER_COLUMNS}
                        """
                    ),
                    {"code": code},
                )
                row = _one_or_none(result)
                if row is None:
                    raise ProviderNotFound("认证源不存在")
                saved = _provider(row)
                await ensure_effective_admin(connection)
                await self._audit(
                    connection,
                    code=code,
                    version=saved.active_version or saved.draft_version,
                    action="disable",
                    result_code="DISABLED",
                    actor=actor,
                    ip=ip,
                )
                return saved
        finally:
            await engine.dispose()

    async def list_role_mappings(
        self,
        code: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                provider = await connection.execute(
                    text("SELECT id FROM auth_provider WHERE code=:code"),
                    {"code": code},
                )
                provider_id = provider.scalar_one_or_none()
                if provider_id is None:
                    raise ProviderNotFound("认证源不存在")
                result = await connection.execute(
                    text(
                        """
                        SELECT external_group,role
                        FROM external_role_mapping
                        WHERE provider_id=:provider_id
                        ORDER BY external_group
                        """
                    ),
                    {"provider_id": int(provider_id)},
                )
                return tuple(
                    ExternalRoleMapping(
                        str(row["external_group"]),
                        cast(Role, str(row["role"])),
                    )
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def replace_role_mappings(
        self,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        *,
        actor: str,
        ip: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await lock_admin_invariant(connection)
                provider = await connection.execute(
                    text(
                        """
                        SELECT id FROM auth_provider
                        WHERE code=:code FOR UPDATE
                        """
                    ),
                    {"code": code},
                )
                provider_id = provider.scalar_one_or_none()
                if provider_id is None:
                    raise ProviderNotFound("认证源不存在")
                await connection.execute(
                    text("DELETE FROM external_role_mapping WHERE provider_id=:provider_id"),
                    {"provider_id": int(provider_id)},
                )
                for mapping in mappings:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO external_role_mapping(provider_id,external_group,role)
                            VALUES(:provider_id,:external_group,:role)
                            """
                        ),
                        {
                            "provider_id": int(provider_id),
                            "external_group": mapping.external_group,
                            "role": mapping.role,
                        },
                    )
                await ensure_effective_admin(connection)
                await self._audit_role_mappings(
                    connection,
                    code=code,
                    mappings=mappings,
                    actor=actor,
                    ip=ip,
                )
                return mappings
        finally:
            await engine.dispose()

    async def _audit_role_mappings(
        self,
        connection: Any,
        *,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        actor: str,
        ip: str,
    ) -> None:
        payload = {
            "provider_code": code,
            "mappings": [
                {"external_group": item.external_group, "role": item.role} for item in mappings
            ],
        }
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(
                  actor,role,ip,action,object_type,object_id,after_val
                ) VALUES(
                  :actor,'admin',CAST(:ip AS inet),'auth_provider_role_mappings_replace',
                  'auth_provider',:code,CAST(:audit AS jsonb)
                )
                """
            ),
            {
                "actor": actor,
                "ip": ip,
                "code": code,
                "audit": json.dumps(payload, ensure_ascii=False),
            },
        )

    async def _audit(
        self,
        connection: Any,
        *,
        code: str,
        version: int,
        action: str,
        result_code: str,
        actor: str,
        ip: str,
    ) -> None:
        payload = {
            "provider_code": code,
            "version": version,
            "action": action,
            "result_code": result_code,
        }
        await connection.execute(
            text(
                """
                INSERT INTO audit_log(
                  actor,role,ip,action,object_type,object_id,after_val
                ) VALUES(
                  :actor,'admin',CAST(:ip AS inet),:audit_action,
                  'auth_provider',:code,CAST(:audit AS jsonb)
                )
                """
            ),
            {
                "actor": actor,
                "ip": ip,
                "audit_action": f"auth_provider_{action}",
                "code": code,
                "audit": json.dumps(payload, ensure_ascii=False),
            },
        )
