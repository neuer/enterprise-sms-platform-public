"""应用管理的 PostgreSQL 事务仓储。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.admin_authorization import AdminAuthorization, hold_admin_authorization
from app.core.errors import ApiError
from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.services.app_contract import AppCreateRequest, AppUpdateRequest, canonical_app_revision
from app.services.app_management import AppNameConflict, AppNotFound
from app.services.callback_authority import ensure_callback_authority_idle
from app.settings import Settings, get_settings

APP_COLUMNS = """
id, name, dept, allowed_categories, default_sign, daily_quota,
rate_limit_per_min, recipient_limit_per_min, segment_limit_per_min,
max_in_flight_chunks, allow_market_api_bulk, blacklist_check, freq_override,
callback_url, allowed_ips, ip_allowlist_exempt_until,
unlimited_quota_exempt_until, admission_exempt_note,
callback_report_enabled, status, created_at, updated_at,
api_key_prefix,
api_key_prev_prefix AS old_key_prefix,
api_key_prev_expires AS old_key_expires_at,
(callback_secret_enc IS NOT NULL) AS callback_secret_configured
"""


def _is_app_name_conflict(error: IntegrityError) -> bool:
    """只把 app_name_key 的 23505 转成领域冲突，其他约束继续失败关闭。"""

    state: str | None = None
    constraint: str | None = None
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if state is None:
            for attr in ("sqlstate", "pgcode"):
                value = getattr(current, attr, None)
                if isinstance(value, str) and value:
                    state = value
                    break
        if constraint is None:
            value = getattr(current, "constraint_name", None)
            if isinstance(value, str) and value:
                constraint = value
            else:
                diag = getattr(current, "diag", None)
                nested = getattr(diag, "constraint_name", None)
                if isinstance(nested, str) and nested:
                    constraint = nested
        current = getattr(current, "orig", None) or current.__cause__
    return state == "23505" and constraint == "app_name_key"


def _safe_row(row: RowMapping) -> dict[str, Any]:
    result = dict(row)
    categories = result.get("allowed_categories")
    if isinstance(categories, str):
        result["allowed_categories"] = categories.split(",")
    return result


class SqlAppRepository:
    """密钥字段从不进入查询响应；写操作与审计同事务提交。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> ManagedAsyncEngine:
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
        after: dict[str, object] | None = None,
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
                rows = await connection.execute(
                    text(
                        f"SELECT {APP_COLUMNS} FROM app "
                        "WHERE usage_subject_kind='api_app' ORDER BY id"
                    )
                )
                return [_safe_row(row) for row in rows.mappings()]
        finally:
            await engine.dispose()

    async def get(self, app_id: int) -> dict[str, Any] | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"SELECT {APP_COLUMNS} FROM app "
                        "WHERE id = :app_id AND usage_subject_kind='api_app'"
                    ),
                    {"app_id": app_id},
                )
                row = result.mappings().one_or_none()
                return _safe_row(row) if row is not None else None
        finally:
            await engine.dispose()

    @asynccontextmanager
    async def _authorized_write(
        self,
        authorization: AdminAuthorization,
        *,
        operation: str,
        app_id: int | None,
        expected_updated_at: datetime | None,
        values: dict[str, object],
    ) -> AsyncIterator[AsyncConnection]:
        """先锁授权主体，再锁回调权限与应用版本；审计失败时整体回滚。"""
        parameters: dict[str, object] = {}
        if operation in {"app_create", "app_update"}:
            model = AppCreateRequest if app_id is None else AppUpdateRequest
            parameters["config"] = {key: values[key] for key in model.model_fields}
        if app_id is not None:
            parameters["expected_updated_at"] = canonical_app_revision(expected_updated_at)
        async with (
            hold_admin_authorization(
                self.settings,
                authorization,
                operation=operation,
                target="new" if app_id is None else str(app_id),
                parameters=parameters,
            ) as connection,
        ):
            if app_id is not None:
                if operation in {"app_update", "app_disable", "app_rotate_callback_secret"}:
                    await ensure_callback_authority_idle(connection, app_id)
                actual = await connection.scalar(
                    text(
                        "SELECT updated_at FROM app WHERE id=:id "
                        "AND usage_subject_kind='api_app' FOR UPDATE"
                    ),
                    {"id": app_id},
                )
                if actual is None:
                    raise AppNotFound("应用不存在")
                if canonical_app_revision(actual) != parameters["expected_updated_at"]:
                    raise ApiError(409, "STATE_CONFLICT", "应用已变化，请刷新后重新认证", None)
            yield connection

    async def create(self, *, authorization: AdminAuthorization, **values: Any) -> int:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_create",
                app_id=None,
                expected_updated_at=None,
                values=values,
            ) as connection:
                try:
                    result = await connection.execute(
                        text(
                            """
                        INSERT INTO app (
                          name, dept, api_key_hash, api_key_prefix,
                          api_key_hash_version, api_key_hash_algorithm,
                          allowed_categories, default_sign, daily_quota,
                          rate_limit_per_min, recipient_limit_per_min,
                          segment_limit_per_min, max_in_flight_chunks,
                          allow_market_api_bulk, blacklist_check, freq_override,
                          allowed_ips, ip_allowlist_exempt_until,
                          unlimited_quota_exempt_until, admission_exempt_note,
                          callback_url, callback_secret_enc,
                          callback_report_enabled, created_by
                        ) VALUES (
                          :name, :dept, :api_key_hash, :api_key_prefix,
                          :api_key_hash_version, :api_key_hash_algorithm,
                          :allowed_categories, :default_sign, :daily_quota,
                          :rate_limit_per_min, :recipient_limit_per_min,
                          :segment_limit_per_min, :max_in_flight_chunks,
                          :allow_market_api_bulk, :blacklist_check,
                          CAST(:freq_override AS jsonb), :allowed_ips,
                          :ip_allowlist_exempt_until,
                          :unlimited_quota_exempt_until, :admission_exempt_note,
                          :callback_url,
                          :callback_secret_enc, :callback_report_enabled, :actor
                        ) RETURNING id
                        """
                        ),
                        values
                        | {
                            "api_key_hash_version": values.get("api_key_hash_version"),
                            "api_key_hash_algorithm": values.get("api_key_hash_algorithm")
                            or (
                                "api_pepper"
                                if values.get("api_key_hash_version") is not None
                                else None
                            ),
                            "freq_override": (
                                json.dumps(values["freq_override"])
                                if values["freq_override"] is not None
                                else None
                            ),
                            "allowed_ips": list(values["allowed_ips"]),
                        },
                    )
                except IntegrityError as error:
                    if _is_app_name_conflict(error):
                        raise AppNameConflict("应用名称已存在") from error
                    raise
                app_id = int(result.scalar_one())
                await self._audit(
                    connection,
                    actor=str(values["actor"]),
                    ip=str(values["ip"]),
                    action="app_create",
                    app_id=app_id,
                    after={
                        "name": values["name"],
                        "allowed_categories": values["allowed_categories"],
                        "daily_quota": values["daily_quota"],
                        "allowed_ips": list(values["allowed_ips"]),
                        "allow_market_api_bulk": values["allow_market_api_bulk"],
                        "has_ip_exemption": values["ip_allowlist_exempt_until"] is not None,
                        "has_quota_exemption": values["unlimited_quota_exempt_until"] is not None,
                    },
                )
                return app_id
        finally:
            await engine.dispose()

    async def update(
        self,
        app_id: int,
        *,
        authorization: AdminAuthorization,
        expected_updated_at: datetime,
        **values: Any,
    ) -> dict[str, Any]:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_update",
                app_id=app_id,
                expected_updated_at=expected_updated_at,
                values=values,
            ) as connection:
                result = await connection.execute(
                    text(
                        f"""
                        UPDATE app SET
                          dept=:dept, allowed_categories=:allowed_categories,
                          default_sign=:default_sign,
                          daily_quota=:daily_quota,
                          rate_limit_per_min=:rate_limit_per_min,
                          recipient_limit_per_min=:recipient_limit_per_min,
                          segment_limit_per_min=:segment_limit_per_min,
                          max_in_flight_chunks=:max_in_flight_chunks,
                          allow_market_api_bulk=:allow_market_api_bulk,
                          blacklist_check=:blacklist_check,
                          freq_override=CAST(:freq_override AS jsonb),
                          allowed_ips=:allowed_ips,
                          ip_allowlist_exempt_until=:ip_allowlist_exempt_until,
                          unlimited_quota_exempt_until=:unlimited_quota_exempt_until,
                          admission_exempt_note=:admission_exempt_note,
                          callback_url=:callback_url,
                          callback_report_enabled=:callback_report_enabled,
                          status=:status, updated_at=now()
                        WHERE id=:app_id AND usage_subject_kind='api_app'
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
                        "allowed_ips": list(values["allowed_ips"]),
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
                    after={
                        "allowed_categories": values["allowed_categories"],
                        "daily_quota": values["daily_quota"],
                        "allowed_ips": list(values["allowed_ips"]),
                        "allow_market_api_bulk": values["allow_market_api_bulk"],
                        "has_ip_exemption": values["ip_allowlist_exempt_until"] is not None,
                        "has_quota_exemption": values["unlimited_quota_exempt_until"] is not None,
                    },
                )
                return _safe_row(row)
        finally:
            await engine.dispose()

    async def disable(
        self,
        app_id: int,
        actor: str,
        ip: str,
        *,
        authorization: AdminAuthorization,
        expected_updated_at: datetime,
    ) -> None:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_disable",
                app_id=app_id,
                expected_updated_at=expected_updated_at,
                values={},
            ) as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET status=0,
                          api_key_hash=:revoked_hash,
                          api_key_prefix='revoked0',
                          api_key_hash_version=NULL,
                          api_key_hash_algorithm='legacy_sha256',
                          api_key_prev_hash=NULL,
                          api_key_prev_prefix=NULL, api_key_prev_expires=NULL,
                          api_key_prev_hash_version=NULL,
                          api_key_prev_hash_algorithm=NULL,
                          updated_at=now()
                        WHERE id=:app_id AND usage_subject_kind='api_app'
                        """
                    ),
                    {
                        "app_id": app_id,
                        "revoked_hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                    },
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

    async def rotate_key(
        self,
        app_id: int,
        *,
        authorization: AdminAuthorization,
        expected_updated_at: datetime,
        **values: Any,
    ) -> None:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_rotate_key",
                app_id=app_id,
                expected_updated_at=expected_updated_at,
                values=values,
            ) as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET
                          api_key_prev_hash=api_key_hash,
                          api_key_prev_prefix=api_key_prefix,
                          api_key_prev_hash_version=api_key_hash_version,
                          api_key_prev_hash_algorithm=api_key_hash_algorithm,
                          api_key_prev_expires=:old_key_expires_at,
                          api_key_hash=:api_key_hash,
                          api_key_prefix=:api_key_prefix,
                          api_key_hash_version=:api_key_hash_version,
                          api_key_hash_algorithm=:api_key_hash_algorithm,
                          updated_at=now()
                        WHERE id=:app_id AND status=1
                          AND usage_subject_kind='api_app'
                        """
                    ),
                    values
                    | {
                        "app_id": app_id,
                        "api_key_hash_version": values.get("api_key_hash_version"),
                        "api_key_hash_algorithm": values.get("api_key_hash_algorithm")
                        or (
                            "api_pepper" if values.get("api_key_hash_version") is not None else None
                        ),
                    },
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

    async def revoke_old_key(
        self,
        app_id: int,
        actor: str,
        ip: str,
        *,
        authorization: AdminAuthorization,
        expected_updated_at: datetime,
    ) -> None:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_revoke_old_key",
                app_id=app_id,
                expected_updated_at=expected_updated_at,
                values={},
            ) as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET api_key_prev_hash=NULL,
                          api_key_prev_prefix=NULL, api_key_prev_expires=NULL,
                          api_key_prev_hash_version=NULL,
                          api_key_prev_hash_algorithm=NULL,
                          updated_at=now()
                        WHERE id=:app_id AND usage_subject_kind='api_app'
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

    async def rotate_callback_secret(
        self,
        app_id: int,
        *,
        authorization: AdminAuthorization,
        expected_updated_at: datetime,
        **values: Any,
    ) -> None:
        engine = self._engine()
        try:
            async with self._authorized_write(
                authorization,
                operation="app_rotate_callback_secret",
                app_id=app_id,
                expected_updated_at=expected_updated_at,
                values=values,
            ) as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE app SET callback_secret_enc=:callback_secret_enc,
                          updated_at=now()
                        WHERE id=:app_id AND status=1
                          AND usage_subject_kind='api_app'
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
