"""跨账号、身份、Provider 与角色映射保护最后一个有效管理员。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.services.user_management import LastAdminProtected

ADMIN_INVARIANT_LOCK_ID = 783_692_261_159_983_410


async def lock_admin_invariant(connection: Any) -> None:
    """串行化所有可能减少有效管理员集合的事务。"""

    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": ADMIN_INVARIANT_LOCK_ID},
    )


async def ensure_effective_admin(connection: Any) -> None:
    """按提交后的 Provider、身份、覆盖角色与目录映射验证管理员可用性。"""

    result = await connection.execute(
        text(
            """
            SELECT ua.id
            FROM user_account ua
            JOIN auth_identity ai ON ai.account_id=ua.id
            JOIN auth_provider ap ON ap.id=ai.provider_id
            WHERE ua.status=1
              AND ai.status=1
              AND ap.enabled=TRUE
              AND (
                (ua.role_override=TRUE AND ua.role='admin')
                OR (
                  ua.role_override=FALSE
                  AND EXISTS(
                    SELECT 1 FROM external_role_mapping erm
                    WHERE erm.provider_id=ap.id
                      AND erm.role='admin'
                      AND erm.external_group=ANY(ai.source_groups)
                  )
                )
              )
            ORDER BY ua.id
            LIMIT 1
            FOR UPDATE OF ua,ai,ap
            """
        )
    )
    if result.scalar_one_or_none() is None:
        raise LastAdminProtected("不能禁用或降级最后一个有效管理员")
