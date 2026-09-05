"""账号锁定与来源封禁状态转换的最小持久安全审计。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.observability import (
    observe_transition_created,
    observe_transition_failure,
    observe_transition_success,
)
from app.core.runtime_resources import bind_connection_system_audit, database_engine
from app.settings import Settings, get_settings

AuthSecurityAction = Literal["auth_account_locked", "auth_ip_banned"]
DeadLetterReason = Literal["missing_hash", "incomplete_envelope", "id_mismatch"]
_PROVIDER_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_DEAD_LETTER_HMAC_KEY = b"sms-auth-transition-dead-letter-v1"
_DEAD_LETTER_REASONS = frozenset(
    {"missing_hash", "incomplete_envelope", "id_mismatch"}
)
_HMAC_HEX = re.compile(r"[0-9a-f]{64}")


def transition_dead_letter_hmac(transition_id: str) -> str:
    """用应用常量对 transition UUID 做 HMAC，供运维死信引用，不是手机号索引。"""

    return hmac.new(
        _DEAD_LETTER_HMAC_KEY,
        transition_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _is_unique_violation(error: BaseException) -> bool:
    """只吞 PostgreSQL 唯一冲突，不能把触发器或权限失败误报成幂等命中。"""

    current: object | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "pgcode", None) or getattr(current, "sqlstate", None)
        if code == "23505":
            return True
        nxt = getattr(current, "orig", None)
        if nxt is None and isinstance(current, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt
    return False


@dataclass(frozen=True, slots=True)
class AuthSecurityTransition:
    """不含用户名、密码或令牌的认证控制状态转换。"""

    action: AuthSecurityAction
    transition_id: str
    provider_code: str
    result_code: Literal["ACCOUNT_LOCKED", "RATE_LIMITED"]
    count: int
    remaining_ttl_seconds: int
    ip: str

    def __post_init__(self) -> None:
        try:
            parsed = UUID(self.transition_id)
        except (ValueError, AttributeError):
            raise ValueError("认证安全转换无效") from None
        if (
            str(parsed) != self.transition_id
            or _PROVIDER_CODE.fullmatch(self.provider_code) is None
            or (self.action, self.result_code)
            not in {
                ("auth_account_locked", "ACCOUNT_LOCKED"),
                ("auth_ip_banned", "RATE_LIMITED"),
            }
            or self.count < 1
            or self.remaining_ttl_seconds < 1
        ):
            raise ValueError("认证安全转换无效")
        try:
            ip_address(self.ip)
        except ValueError:
            raise ValueError("认证安全转换无效") from None


@dataclass(frozen=True, slots=True)
class AuthTransitionDeadLetter:
    """不含用户名、IP 或原始信封的运维死信。"""

    transition_hmac: str
    reason: DeadLetterReason
    field_class: str
    discovered_at: datetime
    build_version: str

    def __post_init__(self) -> None:
        if (
            _HMAC_HEX.fullmatch(self.transition_hmac) is None
            or self.reason not in _DEAD_LETTER_REASONS
            or not self.field_class
            or not self.build_version
            or self.discovered_at.tzinfo is None
        ):
            raise ValueError("认证转换死信无效")


class AuthSecurityEventWriter(Protocol):
    async def ensure_transition(self, transition: AuthSecurityTransition) -> None: ...

    async def record_dead_letter(self, record: AuthTransitionDeadLetter) -> None: ...


class SqlAuthSecurityEventRepository:
    """按 Redis transition UUID 幂等补写锁号与封禁审计。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("auth"))

    async def current_database_user(self) -> str:
        """供启动/集成合同断言实际连接角色，不回显 DSN。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                user = await connection.scalar(text("SELECT current_user"))
        finally:
            await engine.dispose()
        return str(user)

    async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
        payload = {
            "count": transition.count,
            "provider_code": transition.provider_code,
            "remaining_ttl_seconds": transition.remaining_ttl_seconds,
            "result_code": transition.result_code,
            "transition_id": transition.transition_id,
        }
        engine = self._engine()
        observe_transition_created(transition.action)
        try:
            async with engine.begin() as connection:
                await bind_connection_system_audit(
                    connection,
                    actor_name="auth-system",
                    action=transition.action,
                    producer_domain="api",
                )
                # sms_auth 只有 audit_log INSERT、没有 SELECT。冲突推断会要求
                # 表级读取权限，因此保存点内 INSERT VALUES，只吞 unique_violation。
                try:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                """
                                INSERT INTO audit_log(
                                  actor,actor_subject_kind,role,ip,action,object_type,
                                  object_id,after_val
                                ) VALUES(
                                  'auth-system','system',NULL,CAST(:ip AS inet),:action,
                                  'auth_control',:transition_id,CAST(:after AS jsonb)
                                )
                                """
                            ),
                            {
                                "ip": transition.ip,
                                "action": transition.action,
                                "transition_id": transition.transition_id,
                                "after": json.dumps(
                                    payload, sort_keys=True, separators=(",", ":")
                                ),
                            },
                        )
                except IntegrityError as error:
                    if not _is_unique_violation(error):
                        raise
            observe_transition_success(transition.action)
        except Exception as error:
            observe_transition_failure(transition.action)
            raise SessionStateUnavailable("auth security audit unavailable") from error
        finally:
            await engine.dispose()

    async def record_dead_letter(self, record: AuthTransitionDeadLetter) -> None:
        """只写运维死信，绝不补写锁定/封禁审计。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                try:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                """
                                INSERT INTO auth_transition_dead_letter(
                                  transition_hmac,reason,field_class,
                                  discovered_at,build_version
                                ) VALUES(
                                  :hmac,:reason,:field_class,:discovered_at,
                                  :build_version
                                )
                                """
                            ),
                            {
                                "hmac": record.transition_hmac,
                                "reason": record.reason,
                                "field_class": record.field_class,
                                "discovered_at": record.discovered_at,
                                "build_version": record.build_version,
                            },
                        )
                except IntegrityError as error:
                    if not _is_unique_violation(error):
                        raise
        except Exception as error:
            raise SessionStateUnavailable("auth transition dead letter unavailable") from error
        finally:
            await engine.dispose()
