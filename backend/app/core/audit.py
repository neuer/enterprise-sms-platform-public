"""写端点标记、审计载荷防泄漏与同事务写入 helper。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast

from sqlalchemy import text

from app.core.auth.accounts import ActorPrincipal
from app.core.correlation import current_correlation_id

F = TypeVar("F", bound=Callable[..., Any])
PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
FORBIDDEN_AUDIT_KEY = re.compile(
    r"(?:^|_)(?:phone|phones|mobile|mobiles|token|secret|password|"
    r"body|request|request_body|content|ciphertext|encrypted)(?:$|_)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """审计事件只引用稳定主体；actor 名称仅是事件时展示快照。"""

    principal: ActorPrincipal
    action: str
    object_type: str | None = None
    object_id: str | None = None
    role: str | None = None
    ip: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


def validate_audit_payload(value: Any, *, key: str | None = None) -> None:
    """递归拒绝手机号、逐号密文/HMAC、token、secret 与请求正文。"""

    if key is not None and FORBIDDEN_AUDIT_KEY.search(key):
        raise ValueError("audit payload contains a forbidden field")
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            validate_audit_payload(nested_value, key=str(nested_key))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_audit_payload(item, key=key)
        return
    if isinstance(value, str) and PHONE_IN_TEXT.search(value):
        raise ValueError("audit payload contains a phone number")
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("audit payload contains a non-JSON value")


async def insert_audit(connection: Any, event: AuditEvent) -> None:
    """使用调用方连接写入审计；失败向外抛出并回滚同一业务事务。"""

    if not 1 <= len(event.action) <= 48:
        raise ValueError("invalid audit action")
    if event.object_type is not None and len(event.object_type) > 32:
        raise ValueError("invalid audit object_type")
    if event.object_id is not None and len(event.object_id) > 64:
        raise ValueError("invalid audit object_id")
    validate_audit_payload(event.before)
    validate_audit_payload(event.after)
    correlation_id = current_correlation_id()
    assert correlation_id is not None
    await connection.execute(
        text(
            """
            INSERT INTO audit_log(
              correlation_id,actor,actor_subject_kind,actor_account_id,
              actor_identity_id,actor_app_id,role,ip,action,object_type,
              object_id,before_val,after_val
            ) VALUES(
              :correlation_id,:actor,:subject_kind,:account_id,:identity_id,
              :app_id,:role,CAST(:ip AS inet),:action,:object_type,:object_id,
              CAST(:before AS jsonb),CAST(:after AS jsonb)
            )
            """
        ),
        {
            "correlation_id": correlation_id,
            "actor": event.principal.actor_name,
            "subject_kind": event.principal.subject_kind,
            "account_id": event.principal.actor_account_id,
            "identity_id": event.principal.actor_identity_id,
            "app_id": event.principal.actor_app_id,
            "role": event.role,
            "ip": event.ip,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "before": (
                json.dumps(event.before, ensure_ascii=False)
                if event.before is not None
                else None
            ),
            "after": (
                json.dumps(event.after, ensure_ascii=False)
                if event.after is not None
                else None
            ),
        },
    )


def audited(action: str) -> Callable[[F], F]:
    """标记写端点的审计动作且绝不拦截或吞掉业务异常。"""

    def decorate(function: F) -> F:
        @wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            return await function(*args, **kwargs)

        wrapped.__audited_action__ = action  # type: ignore[attr-defined]
        return cast(F, wrapped)

    return decorate
