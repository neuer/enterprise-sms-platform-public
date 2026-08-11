"""敏感正文读取审计；响应返回前持久化最小、无内容载荷的事实。"""

from __future__ import annotations

from typing import Any

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.principal_context import current_audit_principal
from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings


class SensitiveReadAuditor:
    """以独立事务记录敏感读取；审计不可用时调用方必须失败关闭。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def record(
        self,
        *,
        action: str,
        object_type: str,
        object_id: str,
        ip: str,
        count: int,
    ) -> None:
        if count < 1:
            return
        principal = current_audit_principal()
        if principal is None:
            raise RuntimeError("sensitive read audit principal unavailable")
        engine: Any = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=getattr(principal, "role", None),
                        action=action,
                        object_type=object_type,
                        object_id=object_id,
                        ip=ip,
                        after={"result_count": count},
                    ),
                )
        finally:
            await engine.dispose()


def get_sensitive_read_auditor() -> SensitiveReadAuditor:
    return SensitiveReadAuditor(get_settings())
