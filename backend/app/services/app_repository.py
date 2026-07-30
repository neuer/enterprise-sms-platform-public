"""应用管理的 PostgreSQL 事务仓储。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import database_engine
from app.services.app_management import AppNotFound
from app.settings import Settings, get_settings

APP_COLUMNS = """
id, name, dept, allowed_categories, default_sign, daily_quota,
rate_limit_per_min, blacklist_check, freq_override, callback_url,
callback_report_enabled, status, created_at, updated_at,
(callback_secret_enc IS NOT NULL) AS callback_secret_configured
"""


def _safe_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    categories = result.get("allowed_categories")
    if isinstance(categories, str):
        result["allowed_categories"] = categories.split(",")
    return result


class SqlAppRepository:
    """密钥字段从不进入查询响应；写操作与审计同事务提交。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load_security_config(self) -> tuple[int, str]:
        """每次管理请求读取可变安全参数，不从环境变量复制配置。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key, value FROM sys_config
                        WHERE key IN ('key_grace_hours', 'callback_allow_cidrs')
                        """
                    )
                )
                config = {str(row["key"]): str(row["value"]) for row in result.mappings()}
                return (
                    int(config.get("key_grace_hours", "72")),
                    config.get(
                        "callback_allow_cidrs",
                        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
                    ),
                )
        finally:
            await engine.dispose()

    @staticmethod
    async def _audit(
        connection: AsyncConnection,
        *,
        actor: str,
        ip: str,
        action: str,
        app_id: int,
        after: dict[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO audit_log (
                  actor, role, ip, action, object_type, object_id, after_val
                ) VALUES (
                  :actor, 'admin', CAST(:ip AS inet), :action, 'app',
                  CAST(CAST(:app_id AS bigint) AS text), CAST(:after AS jsonb)
                )
                """
            ),
            {
                "actor": actor,
                "ip": ip,
                "action": action,
                "app_id": app_id,
                "after": json.dumps(after, ensure_ascii=False) if after else None,
            },
        )

    async def list(self) -> list[dict[str, Any]]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(text(f"SELECT {APP_COLUMNS} FROM app ORDER BY id"))
                return [_safe_row(row) for row in rows.mappings()]
        finally:
            await engine.dispose()

    async def get(self, app_id: int) -> dict[str, Any] | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT {APP_COLUMNS} FROM app WHERE id = :app_id"),
                    {"app_id": app_id},
                )
                row = result.mappings().one_or_none()
                return _safe_row(row) if row is not None else None
        finally:
            await engine.dispose()

    async def create(self, **values: Any) -> int:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO app (
                          name, dept, api_key_hash, api_key_prefix,
                          allowed_categories, default_sign, daily_quota,
                          rate_limit_per_min, blacklist_check, freq_override,
                          callback_url, callback_secret_enc,
                          callback_report_enabled, created_by
                        ) VALUES (
                          :name, :dept, :api_key_hash, :api_key_prefix,
                          :allowed_categories, :default_sign, :daily_quota,
                          :rate_limit_per_min, :blacklist_check,
                          CAST(:freq_override AS jsonb), :callback_url,
                          :callback_secret_enc, :callback_report_enabled, :actor
                        ) RETURNING id
                        """
                    ),
                    values
                    | {
                        "freq_override": (
                            json.dumps(values["freq_override"])
                            if values["freq_override"] is not None
                            else None
                        )
                    },
                )
                app_id = int(result.scalar_one())
                await self._audit(
                    connection,
                    actor=str(values["actor"]),
                    ip=str(values["ip"]),
                    action="app_create",
                    app_id=app_id,
                    after={"name": values["name"]},
                )
                return app_id
        finally:
            await engine.dispose()

    async def update(self, app_id: int, **values: Any) -> dict[str, Any]:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE app SET
                          dept=:dept, allowed_categories=:allowed_categories,
                          default_sign=:default_sign, daily_quota=:daily_quota,
                          rate_limit_per_min=:rate_limit_per_min,
                          blacklist_check=:blacklist_check,
                          freq_override=CAST(:freq_override AS jsonb),
                          callback_url=:callback_url,
                          callback_report_enabled=:callback_report_enabled,
                          status=:status, updated_at=now()
                        WHERE id=:app_id
                        RETURNING {APP_COLUMNS}
                        """
                    ),
                    values
                    | {
                        "app_id": app_id,
                        "freq_override": (
                            json.dumps(values["freq_override"])
                            if values["freq_override"] is not None
                            else None
                        ),
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise AppNotFound("应用不存在")
                await self._audit(
                    connection,
                    actor=str(values["actor"]),
                    ip=str(values["ip"]),
                    action="app_update",
                    app_id=app_id,
                )
                return _safe_row(row)
        finally:
            await engine.dispose()

    async def disable(self, app_id: int, actor: str, ip: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET status=0, api_key_prev_hash=NULL,
                          api_key_prev_prefix=NULL, api_key_prev_expires=NULL,
                          updated_at=now() WHERE id=:app_id
                        """
                    ),
                    {"app_id": app_id},
                )
                if result.rowcount != 1:
                    raise AppNotFound("应用不存在")
                await self._audit(
                    connection,
                    actor=actor,
                    ip=ip,
                    action="app_disable",
                    app_id=app_id,
                )
        finally:
            await engine.dispose()

    async def rotate_key(self, app_id: int, **values: Any) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET
                          api_key_prev_hash=api_key_hash,
                          api_key_prev_prefix=api_key_prefix,
                          api_key_prev_expires=:old_key_expires_at,
                          api_key_hash=:api_key_hash,
                          api_key_prefix=:api_key_prefix,
                          updated_at=now()
                        WHERE id=:app_id AND status=1
                        """
                    ),
                    values | {"app_id": app_id},
                )
                if result.rowcount != 1:
                    raise AppNotFound("应用不存在或已停用")
                await self._audit(
                    connection,
                    actor=str(values["actor"]),
                    ip=str(values["ip"]),
                    action="app_rotate_key",
                    app_id=app_id,
                    after={"old_key_expires_at": str(values["old_key_expires_at"])},
                )
        finally:
            await engine.dispose()

    async def revoke_old_key(self, app_id: int, actor: str, ip: str) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET api_key_prev_hash=NULL,
                          api_key_prev_prefix=NULL, api_key_prev_expires=NULL,
                          updated_at=now() WHERE id=:app_id
                        """
                    ),
                    {"app_id": app_id},
                )
                if result.rowcount != 1:
                    raise AppNotFound("应用不存在")
                await self._audit(
                    connection,
                    actor=actor,
                    ip=ip,
                    action="app_revoke_old_key",
                    app_id=app_id,
                )
        finally:
            await engine.dispose()

    async def rotate_callback_secret(self, app_id: int, **values: Any) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET callback_secret_enc=:callback_secret_enc,
                          updated_at=now() WHERE id=:app_id AND status=1
                        """
                    ),
                    values | {"app_id": app_id},
                )
                if result.rowcount != 1:
                    raise AppNotFound("应用不存在或已停用")
                await self._audit(
                    connection,
                    actor=str(values["actor"]),
                    ip=str(values["ip"]),
                    action="app_rotate_callback_secret",
                    app_id=app_id,
                )
        finally:
            await engine.dispose()
