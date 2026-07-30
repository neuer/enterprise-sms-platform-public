"""真实联调二次认证与 seal session 的安全审计事实写入。"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings

VendorSecurityAction = Literal["vendor_test_step_up", "vendor_test_seal_session"]
VendorSecurityOutcome = Literal["succeeded", "failed"]
_ACTIONS = frozenset({"vendor_test_step_up", "vendor_test_seal_session"})
_OUTCOMES = frozenset({"succeeded", "failed"})
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class VendorTestSecurityAudit(Protocol):
    async def record(
        self,
        *,
        correlation_id: str,
        principal: SecurityPrincipal,
        action: VendorSecurityAction,
        outcome: VendorSecurityOutcome,
        safe_code: str | None = None,
    ) -> None: ...


def _validate(
    *,
    correlation_id: str,
    principal: SecurityPrincipal,
    action: str,
    outcome: str,
    safe_code: str | None,
) -> None:
    try:
        parsed = UUID(correlation_id)
    except (ValueError, AttributeError):
        raise ValueError("真实联调审计事件无效") from None
    if (
        str(parsed) != correlation_id
        or principal.account_id < 1
        or principal.identity_id < 1
        or action not in _ACTIONS
        or outcome not in _OUTCOMES
        or (safe_code is not None and _SAFE_CODE.fullmatch(safe_code) is None)
    ):
        raise ValueError("真实联调审计事件无效")


class SqlVendorTestSecurityAuditRepository:
    """以独立短事务写入安全元数据，不接收密码、令牌或公钥参数。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def record(
        self,
        *,
        correlation_id: str,
        principal: SecurityPrincipal,
        action: VendorSecurityAction,
        outcome: VendorSecurityOutcome,
        safe_code: str | None = None,
    ) -> None:
        _validate(
            correlation_id=correlation_id,
            principal=principal,
            action=action,
            outcome=outcome,
            safe_code=safe_code,
        )
        payload: dict[str, object] = {
            "correlation_id": correlation_id,
            "count": 1,
            "outcome": outcome,
        }
        if safe_code is not None:
            payload["safe_code"] = safe_code
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,action,object_type,object_id,after_val
                        ) VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,:action,'vendor_test_security',:object_id,
                          CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "role": principal.role,
                        "action": action,
                        "object_id": correlation_id,
                        "after": json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                )
        finally:
            await engine.dispose()
