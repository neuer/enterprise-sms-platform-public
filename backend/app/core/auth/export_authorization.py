"""导出下载提交审计期间的权威主体锁；不扩大 export 数据库角色权限。"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import SecurityPrincipal
from app.core.errors import ApiError
from app.core.runtime_resources import database_engine
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
    business_connection: AsyncConnection,
) -> AsyncGenerator[AsyncConnection, None]:
    """业务事务持有授权锁；认证读取事务先退出，随后才允许提交审计。"""

    engine = database_engine(settings.database_url_for("auth"))
    principal = authorization.principal
    try:
        # 先取得认证池连接，避免业务授权锁与认证写事务形成反向资源等待。
        # 认证事务仍先退出；业务锁由外层事务持有到审计提交。
        async with engine.begin() as connection:
            valid = await business_connection.scalar(text("""
                SELECT public.lock_business_authorization(
                    :account, :identity, :version, :provider, 'export', :role, :dept, :login
                )
            """), {
                "account": principal.account_id, "identity": principal.identity_id,
                "version": authorization.security_version, "provider": authorization.provider_code,
                "role": principal.role, "dept": principal.dept, "login": principal.login_name,
            })
            if not valid:
                raise ApiError(401, "UNAUTHORIZED", "授权状态已变化，请重新登录", None)
            yield connection
    except SQLAlchemyError:
        raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "下载授权状态暂不可用", None) from None
    finally:
        await engine.dispose()
