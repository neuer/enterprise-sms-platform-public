"""导出下载提交审计期间的权威主体锁；不扩大 export 数据库角色权限。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import SecurityPrincipal
from app.core.errors import ApiError
from app.core.runtime_resources import database_engine
from app.services.admin_invariant import lock_admin_invariant
from app.settings import Settings

if TYPE_CHECKING:
    from app.core.auth.jwt import JwtClaims


@dataclass(frozen=True, slots=True)
class ExportDownloadAuthorization:
    principal: SecurityPrincipal
    security_version: int
    provider_code: str

    @classmethod
    def from_claims(cls, claims: JwtClaims) -> ExportDownloadAuthorization:
        """仅由已经验证的访问令牌构造不可变下载授权快照。"""
        return cls(claims.principal, claims.security_version, claims.provider_code)


@asynccontextmanager
async def lock_export_authorization(
    settings: Settings, authorization: ExportDownloadAuthorization,
) -> AsyncIterator[AsyncConnection]:
    """认证角色只持状态锁，导出角色提交审计后才释放；不持锁传输文件。"""

    engine = database_engine(settings.database_url_for("auth"))
    principal = authorization.principal
    try:
        async with engine.begin() as connection:
            await lock_admin_invariant(connection)
            current = await connection.execute(text("""
                SELECT ua.id FROM user_account ua
                JOIN auth_identity ai ON ai.account_id=ua.id
                JOIN auth_provider ap ON ap.id=ai.provider_id
                WHERE ua.id=:account_id AND ai.id=:identity_id
                  AND ua.security_version=:version AND ap.code=:provider
                  AND ua.status=1 AND ai.status=1 AND ap.enabled=TRUE
                  AND ua.role=:role AND ua.dept=:dept AND ai.login_name=:login
                FOR SHARE OF ua,ai,ap
            """), {
                "account_id": principal.account_id, "identity_id": principal.identity_id,
                "version": authorization.security_version, "provider": authorization.provider_code,
                "role": principal.role, "dept": principal.dept, "login": principal.login_name,
            })
            if current.scalar_one_or_none() is None:
                raise ApiError(401, "UNAUTHORIZED", "授权状态已变化，请重新登录", None)
            yield connection
    except SQLAlchemyError:
        raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "下载授权状态暂不可用", None) from None
    finally:
        await engine.dispose()
