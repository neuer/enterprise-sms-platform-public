"""真实联调高风险操作的 Provider 感知单用途二次认证令牌。"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Protocol

from app.core.auth.jwt import JwtClaims

STEP_UP_TTL_SECONDS = 300
ALLOWED_STEP_UP_OPERATIONS = frozenset(
    {
        "install_credentials",
        "rotate_credentials",
        "activate",
        "resume_critical",
        "reset_configuration",
    }
)
_CONSUME_LUA = """
local stored = redis.call('GET', KEYS[1])
if not stored then return 0 end
redis.call('DEL', KEYS[1])
if stored ~= ARGV[1] then return -1 end
return 1
"""


class StepUpExpired(PermissionError):
    """二次认证令牌无效、过期、上下文不符或已消费。"""


class InvalidStepUpOperation(ValueError):
    """拒绝未枚举的高风险操作。"""


class ReauthenticationFacade(Protocol):
    async def reauthenticate_current(
        self,
        claims: JwtClaims,
        password: str,
        ip: str,
    ) -> object | None: ...


class StepUpStore(Protocol):
    async def set(self, key: str, value: str, *, ex: int) -> Any: ...

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...


def _binding(claims: JwtClaims, ip: str, operation: str) -> str:
    return json.dumps(
        {
            "account_id": claims.account_id,
            "identity_id": claims.identity_id,
            "ip": ip,
            "jti": claims.jti,
            "login_name": claims.login_name,
            "operation": operation,
            "provider_code": claims.provider_code,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"vendor-test:step-up:{digest}"


class VendorTestStepUpService:
    """二次认证成功后签发五分钟、绑定上下文且单次消费的 opaque token。"""

    def __init__(self, auth: ReauthenticationFacade, store: StepUpStore) -> None:
        self.auth = auth
        self.store = store

    @staticmethod
    def _require_operation(operation: str) -> None:
        if operation not in ALLOWED_STEP_UP_OPERATIONS:
            raise InvalidStepUpOperation("不支持的二次认证操作")

    async def issue(
        self,
        *,
        claims: JwtClaims,
        password: str,
        ip: str,
        operation: str,
    ) -> str:
        self._require_operation(operation)
        if claims.account_id < 1 or not claims.jti:
            raise StepUpExpired("二次认证上下文无效")
        await self.auth.reauthenticate_current(claims, password, ip)
        token = secrets.token_urlsafe(32)
        await self.store.set(
            _key(token),
            _binding(claims, ip, operation),
            ex=STEP_UP_TTL_SECONDS,
        )
        return token

    async def consume(
        self,
        token: str,
        claims: JwtClaims,
        ip: str,
        operation: str,
    ) -> None:
        try:
            self._require_operation(operation)
        except InvalidStepUpOperation:
            raise StepUpExpired("二次认证令牌无效或已过期") from None
        if not token or claims.account_id < 1 or not claims.jti:
            raise StepUpExpired("二次认证令牌无效或已过期")
        result = await self.store.eval(
            _CONSUME_LUA,
            1,
            _key(token),
            _binding(claims, ip, operation),
        )
        if int(result) != 1:
            raise StepUpExpired("二次认证令牌无效或已过期")
