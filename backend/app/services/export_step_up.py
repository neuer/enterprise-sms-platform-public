"""明文导出下载的账号、会话、IP 与任务绑定单用途二次认证。"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Protocol
from uuid import UUID

from app.core.auth.jwt import JwtClaims

EXPORT_STEP_UP_TTL_SECONDS = 300
_CONSUME_LUA = """
local stored = redis.call('GET', KEYS[1])
if not stored then return 0 end
redis.call('DEL', KEYS[1])
if stored ~= ARGV[1] then return -1 end
return 1
"""


class ExportStepUpExpired(PermissionError):
    """二次认证令牌无效、过期、上下文不符或已被消费。"""


class ReauthenticationFacade(Protocol):
    async def reauthenticate_current(
        self,
        claims: JwtClaims,
        password: str,
        ip: str,
    ) -> None: ...


class StepUpStore(Protocol):
    async def set(self, key: str, value: str, *, ex: int) -> Any: ...

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...


def _binding(claims: JwtClaims, ip: str, public_id: UUID) -> str:
    return json.dumps(
        {
            "account_id": claims.account_id,
            "identity_id": claims.identity_id,
            "ip": ip,
            "jti": claims.jti,
            "operation": "export_decrypted_download",
            "provider_code": claims.provider_code,
            "public_id": str(public_id),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"export:step-up:{digest}"


class ExportStepUpService:
    """签发五分钟有效、绑定稳定主体和具体导出任务的单次令牌。"""

    def __init__(self, auth: ReauthenticationFacade, store: StepUpStore) -> None:
        self.auth = auth
        self.store = store

    @staticmethod
    def _valid_claims(claims: JwtClaims) -> bool:
        return (
            claims.account_id > 0
            and claims.identity_id > 0
            and bool(claims.provider_code)
            and bool(claims.jti)
        )

    async def issue(
        self,
        *,
        claims: JwtClaims,
        password: str,
        ip: str,
        public_id: UUID,
    ) -> str:
        if not self._valid_claims(claims) or claims.role not in {"admin", "approver"}:
            raise ExportStepUpExpired("二次认证上下文无效")
        await self.auth.reauthenticate_current(claims, password, ip)
        token = secrets.token_urlsafe(32)
        await self.store.set(
            _key(token),
            _binding(claims, ip, public_id),
            ex=EXPORT_STEP_UP_TTL_SECONDS,
        )
        return token

    async def consume(
        self,
        token: str,
        *,
        claims: JwtClaims,
        ip: str,
        public_id: UUID,
    ) -> None:
        if not token or not self._valid_claims(claims):
            raise ExportStepUpExpired("二次认证令牌无效或已过期")
        result = await self.store.eval(
            _CONSUME_LUA,
            1,
            _key(token),
            _binding(claims, ip, public_id),
        )
        if int(result) != 1:
            raise ExportStepUpExpired("二次认证令牌无效或已过期")
