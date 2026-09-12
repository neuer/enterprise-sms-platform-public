"""管理员高风险操作的当前 Provider 重认证与用途绑定单次授权。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.core.audit import AuditEvent
from app.core.auth.identity import validate_local_login_name
from app.core.auth.jwt import JwtClaims
from app.core.errors import ApiError
from app.services.auth_provider import LdapProviderConfig
from app.services.export_step_up import ReauthenticationFacade, StepUpStore

ADMIN_STEP_UP_TTL_SECONDS = 300
AdminOperation = Literal[
    "user_create_admin",
    "user_role_change",
    "user_password_reset",
    "user_status_change",
    "provider_role_mapping_change",
    "provider_save_draft",
    "provider_enable_disable",
]
_CONSUME_LUA = """
local stored = redis.call('GET', KEYS[1])
if not stored then return 0 end
redis.call('DEL', KEYS[1])
if stored ~= ARGV[1] then return -1 end
return 1
"""
_FIELDS = {
    "user_create_admin": {"username", "display_name", "dept", "role"},
    "user_role_change": {"role", "role_override"},
    "user_password_reset": {"action"},
    "user_status_change": {"status"},
    "provider_role_mapping_change": {"mappings", "expected_revision"},
    "provider_save_draft": {"config"},
    "provider_enable_disable": {"enabled", "draft_version"},
}


@dataclass(frozen=True, slots=True)
class AdminIntent:
    operation: AdminOperation
    target_id: str
    fingerprint: str


def admin_intent(
    operation: AdminOperation,
    target_id: str,
    parameters: dict[str, Any],
) -> AdminIntent:
    """仅对服务器允许的非凭据参数规范化，不接受客户端自行声明的摘要。"""
    if operation not in _FIELDS or set(parameters) != _FIELDS[operation]:
        raise ApiError(422, "INVALID_PARAM", "二次认证操作参数不完整或包含未知字段", None)
    values = dict(parameters)
    roles = {"admin", "approver", "operator", "viewer"}
    if "role" in values and (not isinstance(values["role"], str) or values["role"] not in roles):
        raise ApiError(422, "INVALID_PARAM", "授权角色无效", None)
    if "role_override" in values and type(values["role_override"]) is not bool:
        raise ApiError(422, "INVALID_PARAM", "授权覆盖状态无效", None)
    if operation == "user_password_reset" and values["action"] != "reset":
        raise ApiError(422, "INVALID_PARAM", "凭据操作无效", None)
    if operation == "user_status_change" and (
        type(values["status"]) is not int or values["status"] not in {0, 1}
    ):
        raise ApiError(422, "INVALID_PARAM", "账号授权状态无效", None)
    if operation == "provider_enable_disable" and (
        type(values["enabled"]) is not bool
        or type(values["draft_version"]) is not int
        or not 1 <= values["draft_version"] <= 2147483647
    ):
        raise ApiError(422, "INVALID_PARAM", "认证源授权版本无效", None)
    if operation == "user_create_admin":
        if target_id != "new" or values["role"] != "admin":
            raise ApiError(422, "INVALID_PARAM", "创建管理员的授权目标无效", None)
        if any(
            not isinstance(values[key], str) or len(values[key]) > bound
            for key, bound in [("username", 64), ("display_name", 128), ("dept", 128)]
        ):
            raise ApiError(422, "INVALID_PARAM", "账号授权参数无效", None)
        values["username"] = validate_local_login_name(values["username"])
        values["display_name"] = str(values["display_name"]).strip()
        values["dept"] = str(values["dept"]).strip()
    elif operation.startswith("user_"):
        if not target_id.isascii() or not target_id.isdecimal() or int(target_id) <= 0:
            raise ApiError(422, "INVALID_PARAM", "账号授权目标无效", None)
        target_id = str(int(target_id))
    elif not target_id or len(target_id) > 64:
        raise ApiError(422, "INVALID_PARAM", "认证源授权目标无效", None)
    if operation == "provider_save_draft":
        values["config"] = LdapProviderConfig.from_mapping(values["config"]).to_mapping()
    if operation == "provider_role_mapping_change":
        revision = values["expected_revision"]
        if (
            not isinstance(revision, str)
            or len(revision) != 64
            or any(c not in "0123456789abcdef" for c in revision)
        ):
            raise ApiError(422, "INVALID_PARAM", "映射授权版本无效", None)
        mappings = values["mappings"]
        if not isinstance(mappings, list) or len(mappings) > 100:
            raise ApiError(422, "INVALID_PARAM", "映射授权参数无效", None)
        normalized = []
        for item in mappings:
            if not isinstance(item, dict) or set(item) != {"external_group", "role", "dept"}:
                raise ApiError(422, "INVALID_PARAM", "映射授权参数无效", None)
            if (
                not isinstance(item["role"], str)
                or item["role"] not in roles
                or not isinstance(item["external_group"], str)
                or not 1 <= len(item["external_group"]) <= 256
                or not isinstance(item["dept"], str)
                or not 1 <= len(item["dept"]) <= 128
            ):
                raise ApiError(422, "INVALID_PARAM", "映射授权参数无效", None)
            normalized.append(
                {
                    "external_group": str(item["external_group"]).strip(),
                    "role": item["role"],
                    "dept": str(item["dept"] or "").strip(),
                }
            )
        values["mappings"] = sorted(normalized, key=lambda item: item["external_group"])
    encoded = json.dumps(
        values, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(encoded) > 65536:
        raise ApiError(422, "INVALID_PARAM", "二次认证操作参数过大", None)
    return AdminIntent(operation, target_id, hashlib.sha256(encoded.encode()).hexdigest())


def _binding(claims: JwtClaims, ip: str, intent: AdminIntent) -> str:
    return json.dumps(
        {
            "account_id": claims.account_id,
            "identity_id": claims.identity_id,
            "provider_code": claims.provider_code,
            "security_version": claims.security_version,
            "jti": claims.jti,
            "ip": ip,
            "operation": intent.operation,
            "target_id": intent.target_id,
            "fingerprint": intent.fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _key(token: str) -> str:
    return "auth:admin-step-up:" + hashlib.sha256(token.encode()).hexdigest()


class AdminReauthenticationFacade(ReauthenticationFacade, Protocol):
    async def verify(self, token: str) -> JwtClaims: ...


class AdminStepUpService:
    """签发和消费均失败关闭；消费先于业务副作用，失败后不恢复令牌。"""

    def __init__(
        self,
        auth: AdminReauthenticationFacade,
        store: StepUpStore,
        audit_sink: Callable[[AuditEvent], Awaitable[None]],
    ) -> None:
        self.auth = auth
        self.store = store
        self.audit_sink = audit_sink

    @staticmethod
    def _require_admin(claims: JwtClaims) -> None:
        if (
            claims.role != "admin"
            or claims.account_id <= 0
            or claims.identity_id <= 0
            or not claims.jti
        ):
            raise ApiError(403, "FORBIDDEN", "仅当前有效管理员可操作", None)

    async def _audit(self, claims: JwtClaims, ip: str, intent: AdminIntent, result: str) -> None:
        await self.audit_sink(
            AuditEvent(
                principal=claims.principal,
                action="admin_step_up",
                object_type="admin_operation",
                object_id=intent.target_id,
                role=claims.role,
                ip=ip,
                after={"operation": intent.operation, "result": result},
            )
        )

    async def issue(self, *, claims: JwtClaims, password: str, ip: str, intent: AdminIntent) -> str:
        self._require_admin(claims)
        try:
            await self.auth.reauthenticate_current(claims, password, ip)
        except Exception:
            await self._audit(claims, ip, intent, "reauthentication_failed")
            raise
        token = secrets.token_urlsafe(32)
        key = _key(token)
        try:
            if not await self.store.set(
                key, _binding(claims, ip, intent), ex=ADMIN_STEP_UP_TTL_SECONDS
            ):
                raise RuntimeError("step-up store unavailable")
            await self._audit(claims, ip, intent, "issued")
        except Exception:
            try:
                await self.store.delete(key)
            finally:
                raise ApiError(
                    503, "AUTH_SESSION_UNAVAILABLE", "二次认证授权暂不可用", None
                ) from None
        return token

    async def consume(
        self,
        token: str | None,
        *,
        claims: JwtClaims,
        access_token: str,
        ip: str,
        intent: AdminIntent,
    ) -> None:
        self._require_admin(claims)
        if not token:
            await self._audit(claims, ip, intent, "required")
            raise ApiError(401, "STEP_UP_REQUIRED", "该操作需要重新验证当前账号", None)
        try:
            result = await self.store.eval(
                _CONSUME_LUA, 1, _key(token), _binding(claims, ip, intent)
            )
        except Exception:
            await self._audit(claims, ip, intent, "unavailable")
            raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "二次认证授权暂不可用", None) from None
        if int(result) != 1:
            await self._audit(claims, ip, intent, "rejected")
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证已失效，请重新验证当前账号", None)
        await self._audit(claims, ip, intent, "consumed")
        current = await self.auth.verify(access_token)
        self._require_admin(current)
        if _binding(current, ip, intent) != _binding(claims, ip, intent):
            raise ApiError(401, "STEP_UP_REQUIRED", "二次认证主体已变化", None)
