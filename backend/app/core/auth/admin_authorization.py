"""高风险写入的不可变授权快照与短事务内复核。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import ApiError
from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.settings import Settings

if TYPE_CHECKING:
    from app.core.auth.jwt import JwtClaims
    from app.services.admin_step_up import AdminIntent


@dataclass(frozen=True, slots=True)
class AdminAuthorization:
    account_id: int
    identity_id: int
    security_version: int
    provider_code: str
    intent: AdminIntent | None

    @classmethod
    def from_claims(
        cls, claims: JwtClaims, intent: AdminIntent | None = None
    ) -> AdminAuthorization:
        """只从服务器已验证的当前主体构造授权。"""
        return cls(
            claims.account_id,
            claims.identity_id,
            claims.security_version,
            claims.provider_code,
            intent,
        )


def require_admin_intent(
    authorization: AdminAuthorization,
    *,
    operation: str,
    target: str,
    parameters: dict[str, Any],
) -> None:
    """实际写入的规范化非凭据参数必须与单次授权一致。"""
    from app.services.admin_step_up import admin_intent

    if authorization.intent is None:
        valid = (
            operation == "user_create"
            and target == "new"
            and set(parameters) == {"username", "display_name", "dept", "role"}
            and parameters["role"] in {"approver", "operator", "viewer"}
        )
    else:
        valid = (
            authorization.intent.operation == operation
            and authorization.intent.target_id == target
            and authorization.intent
            == admin_intent(
                authorization.intent.operation,
                target,
                parameters,
            )
        )
    if not valid:
        raise ApiError(401, "STEP_UP_REQUIRED", "二次认证用途或参数不匹配", None)


async def lock_admin_authorization(
    connection: AsyncConnection,
    authorization: AdminAuthorization,
    *,
    operation: str,
    target: str,
    parameters: dict[str, Any],
    authority_lock: Literal["update", "share"] = "update",
) -> None:
    """比对实际意图后先取统一锁，再锁定管理员；撤销先提交时写入失败。"""
    from app.services.admin_invariant import lock_admin_invariant

    require_admin_intent(authorization, operation=operation, target=target, parameters=parameters)
    await lock_admin_invariant(connection)
    lock_clause = "FOR SHARE OF ua,ai,ap" if authority_lock == "share" else "FOR UPDATE OF ua,ai,ap"
    result = await connection.execute(
        text(
            """
        SELECT ua.id FROM user_account ua
        JOIN auth_identity ai ON ai.account_id=ua.id
        JOIN auth_provider ap ON ap.id=ai.provider_id
        WHERE ua.id=:account_id AND ai.id=:identity_id
          AND ua.security_version=:version AND ap.code=:provider
          AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
          AND ua.role='admin'
          AND (ap.kind='local' OR (
            SELECT count(DISTINCT btrim(erm.dept)) FROM external_role_mapping erm
            WHERE erm.provider_id=ap.id AND erm.external_group=ANY(ai.source_groups)
              AND NULLIF(btrim(erm.dept),'') IS NOT NULL
          )=1)
          AND (ua.role_override=TRUE OR EXISTS (
            SELECT 1 FROM external_role_mapping erm WHERE erm.provider_id=ap.id
              AND erm.external_group=ANY(ai.source_groups) AND erm.role='admin'
          ))
    """
            + lock_clause
        ),
        {
            "account_id": authorization.account_id,
            "identity_id": authorization.identity_id,
            "version": authorization.security_version,
            "provider": authorization.provider_code,
        },
    )
    if result.scalar_one_or_none() is None:
        raise ApiError(401, "STEP_UP_REQUIRED", "管理员权限已变化，请重新登录并验证", None)


@asynccontextmanager
async def hold_admin_authorization(
    settings: Settings,
    authorization: AdminAuthorization,
    *,
    operation: str,
    target: str,
    parameters: dict[str, Any],
    engine: ManagedAsyncEngine | None = None,
) -> AsyncIterator[AsyncConnection]:
    """授权锁、业务写入与审计共用业务角色事务，不授予身份表访问权限。"""
    owns_engine = engine is None
    engine = engine if engine is not None else database_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            require_admin_intent(
                authorization, operation=operation, target=target, parameters=parameters
            )
            try:
                valid = await connection.scalar(
                    text("""
                    SELECT public.lock_business_authorization(
                        :account, :identity, :version, :provider, 'admin',
                        'admin', NULL, NULL
                    )
                """),
                    {
                        "account": authorization.account_id,
                        "identity": authorization.identity_id,
                        "version": authorization.security_version,
                        "provider": authorization.provider_code,
                    },
                )
            except SQLAlchemyError:
                raise ApiError(
                    503, "AUTH_SESSION_UNAVAILABLE", "管理操作授权暂不可用", None
                ) from None
            if not valid:
                raise ApiError(401, "STEP_UP_REQUIRED", "管理员权限已变化，请重新登录并验证", None)
            yield connection
    except SQLAlchemyError:
        raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "管理操作授权暂不可用", None) from None
    finally:
        if owns_engine:
            await engine.dispose()
