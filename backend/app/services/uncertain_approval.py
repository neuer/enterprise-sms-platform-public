"""从不可修改的双人确认审计恢复重发审批申请人，不冒充系统发送主体。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.auth.accounts import SecurityPrincipal
from app.services.uncertain_resolution import UncertainResolution, UncertainResolutionConflict


async def load_approval_applicant(
    connection: AsyncConnection, resolution: UncertainResolution,
) -> SecurityPrincipal:
    """调用方已持有来源/决议锁；身份只能来自同一 generation 的唯一确认事实。"""

    rows = (await connection.execute(text("""
        SELECT account_id,identity_id,login_name
        FROM uncertain_resend_approval_applicant
        WHERE resolution_id=:id AND effect_generation=:generation
          AND account_id=:confirmer
    """), {
        "id": resolution.id, "generation": resolution.effect_generation,
        "confirmer": resolution.confirmer_account_id,
    })).mappings().all()
    if len(rows) != 1 or not resolution.source_dept:
        raise UncertainResolutionConflict("重发审批缺少唯一的已确认身份")
    row = rows[0]
    return SecurityPrincipal(
        int(row["account_id"]), int(row["identity_id"]), str(row["login_name"]),
        resolution.source_dept, "admin",
    )
