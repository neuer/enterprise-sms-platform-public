"""敏感词命中审计复用稳定认证主体，受控重发须再次验证决议。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import (
    ActorPrincipal,
    ApplicationPrincipal,
    SecurityPrincipal,
    UncertainEffectPrincipal,
)
from app.core.runtime_resources import bind_connection_system_audit


async def audit_sensitive_hit(
    connection: AsyncConnection, principal: ActorPrincipal, hit_count: int,
) -> None:
    """只记录命中数量与决议代次，不把敏感词或发送正文写入审计。"""

    after: dict[str, object] = {"hit_count": hit_count}
    if isinstance(principal, UncertainEffectPrincipal):
        from app.services.uncertain_resolution import lock_uncertain_resend

        await lock_uncertain_resend(connection, principal)
        await bind_connection_system_audit(
            connection, actor_name=principal.actor_name, action="sensitive_hit",
        )
        object_type, object_id = "sms_uncertain_resolution", principal.resolution_id
        after["effect_generation"] = principal.effect_generation
    elif isinstance(principal, SecurityPrincipal):
        object_type, object_id = "user_account", principal.account_id
    elif isinstance(principal, ApplicationPrincipal):
        object_type, object_id = "app", principal.app_id
    else:
        raise ValueError("敏感词审计主体无效")
    await insert_audit(connection, AuditEvent(
        principal=principal,
        role=principal.role if isinstance(principal, SecurityPrincipal) else None,
        action="sensitive_hit", object_type=object_type, object_id=str(object_id),
        after=after,
    ))
