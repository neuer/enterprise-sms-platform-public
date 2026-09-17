"""高风险写入的不可变授权快照与短事务内复核。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from app.core.errors import ApiError

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


async def lock_admin_authorization(
    connection: Any,
    authorization: AdminAuthorization,
    *,
    operation: str,
    target: str,
) -> None:
    """先取统一锁，再锁定当前管理员；撤销先提交时整笔写入失败。"""
    from app.services.admin_invariant import lock_admin_invariant

    await lock_admin_invariant(connection)
    if authorization.intent is None:
        intent_valid = operation == "user_create"
    else:
        intent_valid = (
            authorization.intent.operation == operation and authorization.intent.target_id == target
        )
    if not intent_valid:
        raise ApiError(401, "STEP_UP_REQUIRED", "二次认证用途不匹配", None)
    result = await connection.execute(
        text("""
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
        FOR UPDATE OF ua,ai,ap
    """),
        {
            "account_id": authorization.account_id,
            "identity_id": authorization.identity_id,
            "version": authorization.security_version,
            "provider": authorization.provider_code,
        },
    )
    if result.scalar_one_or_none() is None:
        raise ApiError(401, "STEP_UP_REQUIRED", "管理员权限已变化，请重新登录并验证", None)
