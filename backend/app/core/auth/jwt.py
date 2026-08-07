"""数据库权威安全上下文、短访问令牌与单次轮换 refresh token。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import jwt

from app.core.auth.accounts import PlatformAccount, SecurityPrincipal
from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.roles import Role
from app.core.auth.service import AsyncKeyValue

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
PASSWORD_CHANGE_TOKEN_TYPE = "password_change"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=7)
PASSWORD_CHANGE_TTL = timedelta(minutes=10)
JWT_ISSUER = "sms-platform-web"
JWT_AUDIENCE = "sms-platform-api"
JWT_KEY_VERSION_BYTES = 2

LOGGER = logging.getLogger(__name__)

_ROTATE_REFRESH_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
if current ~= ARGV[1] then
  redis.call('DEL', KEYS[1])
  return -1
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


@dataclass(frozen=True, slots=True, init=False)
class JwtClaims:
    """访问令牌声明；sub 固定为 user_account.id。"""

    account_id: int
    identity_id: int
    provider_code: str
    login_name: str
    display_name: str
    dept: str
    role: Role
    security_version: int
    jti: str
    session_id: str

    def __init__(
        self,
        account_id: int | str,
        identity_id: int | str,
        provider_code: str,
        login_name: str,
        display_name: str = "",
        dept: str = "",
        role: Role = "viewer",
        security_version: int = 1,
        jti: str = "",
        session_id: str = "",
    ) -> None:
        # 旧的四位置参数仅保留给尚未迁移的 API 权限单测；JwtService 拒绝签发。
        if isinstance(account_id, str):
            legacy_role = login_name
            object.__setattr__(self, "account_id", 0)
            object.__setattr__(self, "identity_id", 0)
            object.__setattr__(self, "provider_code", "legacy")
            object.__setattr__(self, "login_name", account_id)
            object.__setattr__(self, "display_name", str(identity_id))
            object.__setattr__(self, "dept", provider_code)
            object.__setattr__(self, "role", cast(Role, legacy_role))
        else:
            object.__setattr__(self, "account_id", account_id)
            object.__setattr__(self, "identity_id", int(identity_id))
            object.__setattr__(self, "provider_code", provider_code)
            object.__setattr__(self, "login_name", login_name)
            object.__setattr__(self, "display_name", display_name)
            object.__setattr__(self, "dept", dept)
            object.__setattr__(self, "role", role)
        object.__setattr__(self, "security_version", security_version)
        object.__setattr__(self, "jti", jti)
        object.__setattr__(self, "session_id", session_id)

    @property
    def username(self) -> str:
        """兼容只读展示调用；资源定位必须使用 account_id。"""

        return self.login_name

    @property
    def principal(self) -> SecurityPrincipal:
        """把权威 JWT 投影转换为服务层唯一允许的安全主体。"""

        return SecurityPrincipal(
            account_id=self.account_id,
            identity_id=self.identity_id,
            login_name=self.login_name,
            dept=self.dept,
            role=self.role,
        )


@dataclass(frozen=True, slots=True)
class IssuedTokenPair:
    token: str
    refresh_token: str
    expires_in: int = int(ACCESS_TOKEN_TTL.total_seconds())
    refresh_expires_in: int = int(REFRESH_TOKEN_TTL.total_seconds())


@dataclass(frozen=True, slots=True)
class PasswordChangeClaims:
    account_id: int
    identity_id: int
    provider_code: str
    login_name: str
    jti: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class JwtKeyring:
    """版本化 JWT 签名 keyring：一个活动签名 key 加受限历史验证 key。"""

    active_version: int
    keys: dict[int, bytes]


def _decode_jwt_key(encoded: object, *, version: int) -> bytes:
    if not isinstance(encoded, str):
        raise ValueError(f"JWT key version {version} must be base64 text")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(f"JWT key version {version} is not valid base64") from None
    if len(key) < 32:
        raise ValueError(f"JWT key version {version} must decode to at least 32 bytes")
    return key


def parse_jwt_keyring(secret: str) -> JwtKeyring:
    """解析裸 v1 key 或版本化 JSON keyring；新签发必须使用带 kid 的 key。"""

    if not secret.startswith("{"):
        return JwtKeyring(1, {1: secret.encode("utf-8")})
    try:
        document = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise ValueError("JWT keyring is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("JWT keyring must be an object")
    active = document.get("active_version")
    raw_keys = document.get("keys")
    if not isinstance(active, int) or isinstance(active, bool) or active < 1:
        raise ValueError("JWT keyring active_version must be a positive integer")
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise ValueError("JWT keyring keys must be a non-empty object")
    keys: dict[int, bytes] = {}
    for raw_version, encoded in raw_keys.items():
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise ValueError("JWT keyring version must be an integer") from None
        if str(version) != str(raw_version) or version < 1:
            raise ValueError(f"JWT keyring version is invalid: {raw_version}")
        keys[version] = _decode_jwt_key(encoded, version=version)
    if active not in keys:
        raise ValueError("JWT keyring active key version is missing")
    return JwtKeyring(active, keys)


def utc_now() -> datetime:
    return datetime.now(UTC)


class JwtService:
    """短期 HS256 访问令牌与服务端单次轮换 refresh token。"""

    def __init__(
        self,
        secret: str,
        store: AsyncKeyValue,
        *,
        accept_legacy: bool = True,
        clock: Callable[[], datetime] = utc_now,
        ttl: timedelta = ACCESS_TOKEN_TTL,
        refresh_ttl: timedelta = REFRESH_TOKEN_TTL,
        security_session_loader: (
            Callable[[int, int], Awaitable[PlatformAccount]] | None
        ) = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        if ttl.total_seconds() <= 0 or ttl > timedelta(minutes=15):
            raise ValueError("JWT access ttl must be between 1 second and 15 minutes")
        if refresh_ttl < timedelta(minutes=1) or refresh_ttl > timedelta(days=7):
            raise ValueError("JWT refresh ttl must be between 1 minute and 7 days")
        self.secret = secret
        self._keyring = parse_jwt_keyring(secret)
        self.accept_legacy = accept_legacy
        self.store = store
        self.clock = clock
        self.ttl = ttl
        self.refresh_ttl = refresh_ttl
        self.security_session_loader = security_session_loader

    @staticmethod
    def _require_stable(claims: JwtClaims) -> None:
        if (
            claims.account_id < 1
            or claims.identity_id < 1
            or not claims.provider_code
            or not claims.login_name
            or claims.security_version < 1
        ):
            raise ValueError("token requires stable account, identity and security claims")

    def _common_payload(
        self,
        claims: JwtClaims,
        *,
        token_type: str,
        session_id: str,
        ttl: timedelta,
    ) -> dict[str, Any]:
        self._require_stable(claims)
        now = self.clock()
        return {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": str(claims.account_id),
            "identity_id": claims.identity_id,
            "provider_code": claims.provider_code,
            "login_name": claims.login_name,
            "display_name": claims.display_name,
            "dept": claims.dept,
            "role": claims.role,
            "security_version": claims.security_version,
            "token_type": token_type,
            "sid": session_id,
            "jti": uuid4().hex,
            "iat": now.timestamp(),
            "exp": int((now + ttl).timestamp()),
        }

    def _encode_access(self, claims: JwtClaims, session_id: str) -> str:
        return jwt.encode(
            self._common_payload(
                claims,
                token_type=ACCESS_TOKEN_TYPE,
                session_id=session_id,
                ttl=self.ttl,
            ),
            self._signing_key(),
            algorithm="HS256",
            headers={"kid": str(self._keyring.active_version)},
        )

    def _encode_refresh(
        self,
        claims: JwtClaims,
        session_id: str,
        family_expires_at: int,
    ) -> tuple[str, dict[str, Any]]:
        payload = self._common_payload(
            claims,
            token_type=REFRESH_TOKEN_TYPE,
            session_id=session_id,
            ttl=self.refresh_ttl,
        )
        payload["family_exp"] = family_expires_at
        payload["exp"] = family_expires_at
        return (
            jwt.encode(
                payload,
                self._signing_key(),
                algorithm="HS256",
                headers={"kid": str(self._keyring.active_version)},
            ),
            payload,
        )

    def _signing_key(self) -> bytes:
        """当前活动签名 key；旧版本仅保留在验证 keyring 中。"""

        return self._keyring.keys[self._keyring.active_version]

    def issue(self, claims: JwtClaims) -> str:
        """仅供不需要 refresh 的内部测试/窄流程签发短访问令牌。"""

        return self._encode_access(claims, uuid4().hex)

    @staticmethod
    def _refresh_key(session_id: str) -> str:
        return f"auth:jwt:refresh-family:{session_id}"

    @staticmethod
    def _refresh_binding(payload: dict[str, Any]) -> str:
        return json.dumps(
            {
                "account_id": int(payload["sub"]),
                "identity_id": int(payload["identity_id"]),
                "jti": str(payload["jti"]),
                "provider_code": str(payload["provider_code"]),
                "security_version": int(payload["security_version"]),
                "family_exp": int(payload["family_exp"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    async def issue_pair(self, claims: JwtClaims) -> IssuedTokenPair:
        session_id = uuid4().hex
        access_token = self._encode_access(claims, session_id)
        family_expires_at = int((self.clock() + self.refresh_ttl).timestamp())
        refresh_token, refresh_payload = self._encode_refresh(
            claims,
            session_id,
            family_expires_at,
        )
        remaining = max(1, family_expires_at - int(self.clock().timestamp()))
        try:
            await self.store.set(
                self._refresh_key(session_id),
                self._refresh_binding(refresh_payload),
                ex=remaining,
            )
        except Exception:
            raise SessionStateUnavailable("refresh token state unavailable") from None
        return IssuedTokenPair(
            access_token,
            refresh_token,
            refresh_expires_in=remaining,
        )

    def issue_password_change(
        self,
        *,
        account_id: int,
        identity_id: int,
        provider_code: str,
        login_name: str,
    ) -> str:
        if account_id < 1 or identity_id < 1 or provider_code != "local":
            raise ValueError("password change token requires a local identity")
        now = self.clock()
        payload = {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": str(account_id),
            "identity_id": identity_id,
            "provider_code": provider_code,
            "login_name": login_name,
            "token_type": PASSWORD_CHANGE_TOKEN_TYPE,
            "jti": uuid4().hex,
            "iat": now.timestamp(),
            "exp": int((now + PASSWORD_CHANGE_TTL).timestamp()),
        }
        return jwt.encode(
            payload,
            self._signing_key(),
            algorithm="HS256",
            headers={"kid": str(self._keyring.active_version)},
        )

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidCredentials("无效或已吊销的令牌") from None
        kid = header.get("kid")
        if kid is None:
            if not self.accept_legacy:
                raise InvalidCredentials("无效或已吊销的令牌")
            key = self._keyring.keys.get(1) or self._keyring.keys[
                self._keyring.active_version
            ]
            LOGGER.warning("legacy JWT without kid accepted")
        else:
            try:
                raw_kid = str(kid)
                if not raw_kid.isdigit() or str(int(raw_kid)) != raw_kid:
                    raise ValueError("invalid kid")
                key = self._keyring.keys[int(raw_kid)]
            except (TypeError, ValueError, KeyError):
                raise InvalidCredentials("无效或已吊销的令牌") from None
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
        except jwt.PyJWTError:
            raise InvalidCredentials("无效或已吊销的令牌") from None
        if kid is not None and (
            payload.get("iss") != JWT_ISSUER or payload.get("aud") != JWT_AUDIENCE
        ):
            raise InvalidCredentials("无效或已吊销的令牌")
        if not {"sub", "token_type", "jti", "iat", "exp"}.issubset(payload):
            raise InvalidCredentials("无效或已吊销的令牌")
        try:
            expires_at = int(payload["exp"])
        except (TypeError, ValueError):
            raise InvalidCredentials("无效或已吊销的令牌") from None
        if expires_at <= int(self.clock().timestamp()):
            raise InvalidCredentials("无效或已吊销的令牌")
        return payload

    @staticmethod
    def _stable_ids(payload: dict[str, Any]) -> tuple[int, int]:
        try:
            account_id = int(payload["sub"])
            identity_id = int(payload["identity_id"])
        except (KeyError, TypeError, ValueError):
            raise InvalidCredentials("无效或已吊销的令牌") from None
        if account_id < 1 or identity_id < 1:
            raise InvalidCredentials("无效或已吊销的令牌")
        return account_id, identity_id

    def _claims(self, payload: dict[str, Any]) -> JwtClaims:
        required = {
            "identity_id",
            "provider_code",
            "login_name",
            "display_name",
            "dept",
            "role",
            "security_version",
            "sid",
        }
        if not required.issubset(payload):
            raise InvalidCredentials("无效或已吊销的令牌")
        account_id, identity_id = self._stable_ids(payload)
        role = str(payload["role"])
        if role not in {"admin", "approver", "operator", "viewer"}:
            raise InvalidCredentials("无效或已吊销的令牌")
        try:
            security_version = int(payload["security_version"])
        except (TypeError, ValueError):
            raise InvalidCredentials("无效或已吊销的令牌") from None
        session_id = str(payload["sid"])
        if security_version < 1 or not session_id:
            raise InvalidCredentials("无效或已吊销的令牌")
        return JwtClaims(
            account_id=account_id,
            identity_id=identity_id,
            provider_code=str(payload["provider_code"]),
            login_name=str(payload["login_name"]),
            display_name=str(payload["display_name"]),
            dept=str(payload["dept"]),
            role=cast(Role, role),
            security_version=security_version,
            jti=str(payload["jti"]),
            session_id=session_id,
        )

    async def _authoritative(self, claims: JwtClaims) -> JwtClaims:
        if self.security_session_loader is None:
            return claims
        try:
            current = await self.security_session_loader(
                claims.account_id,
                claims.identity_id,
            )
        except (InvalidCredentials, LookupError):
            raise InvalidCredentials("无效或已吊销的令牌") from None
        except SessionStateUnavailable:
            raise
        except Exception:
            raise SessionStateUnavailable("security session projection unavailable") from None
        if (
            not current.active
            or current.account_id != claims.account_id
            or current.identity_id != claims.identity_id
            or current.provider_code != claims.provider_code
            or current.login_name != claims.login_name
            or current.dept != claims.dept
            or current.role != claims.role
            or current.security_version != claims.security_version
        ):
            raise InvalidCredentials("无效或已吊销的令牌")
        return JwtClaims(
            account_id=current.account_id,
            identity_id=current.identity_id,
            provider_code=current.provider_code,
            login_name=current.login_name,
            display_name=current.display_name,
            dept=current.dept,
            role=current.role,
            security_version=current.security_version,
            jti=claims.jti,
            session_id=claims.session_id,
        )

    async def _ensure_account_not_revoked(
        self,
        payload: dict[str, Any],
        claims: JwtClaims,
    ) -> None:
        try:
            revoked_after = await self.store.get(
                f"auth:jwt:account-revoked:{claims.account_id}"
            )
            revoked_at = float(revoked_after) if revoked_after is not None else None
            issued_at = float(payload["iat"])
        except Exception:
            raise SessionStateUnavailable("JWT revocation state unavailable") from None
        if revoked_at is not None and issued_at <= revoked_at:
            raise InvalidCredentials("无效或已吊销的令牌")

    async def verify(self, token: str) -> JwtClaims:
        payload = self._decode(token)
        if payload["token_type"] != ACCESS_TOKEN_TYPE:
            raise InvalidCredentials("无效或已吊销的令牌")
        claims = self._claims(payload)
        try:
            if await self.store.get(f"auth:jwt:revoked:{claims.jti}") is not None:
                raise InvalidCredentials("无效或已吊销的令牌")
        except InvalidCredentials:
            raise
        except Exception:
            raise SessionStateUnavailable("JWT revocation state unavailable") from None
        await self._ensure_account_not_revoked(payload, claims)
        return await self._authoritative(claims)

    async def rotate_refresh(self, token: str) -> IssuedTokenPair:
        payload = self._decode(token)
        if payload["token_type"] != REFRESH_TOKEN_TYPE:
            raise InvalidCredentials("无效或已使用的刷新令牌")
        try:
            family_expires_at = int(payload["family_exp"])
        except (KeyError, TypeError, ValueError):
            raise InvalidCredentials("无效或已使用的刷新令牌") from None
        remaining = family_expires_at - int(self.clock().timestamp())
        if family_expires_at != int(payload["exp"]) or remaining < 1:
            raise InvalidCredentials("无效或已使用的刷新令牌")
        claims = self._claims(payload)
        await self._ensure_account_not_revoked(payload, claims)
        claims = await self._authoritative(claims)
        session_id = claims.session_id
        new_access = self._encode_access(claims, session_id)
        new_refresh, new_payload = self._encode_refresh(
            claims,
            session_id,
            family_expires_at,
        )
        try:
            result = await self.store.eval(
                _ROTATE_REFRESH_LUA,
                1,
                self._refresh_key(session_id),
                self._refresh_binding(payload),
                self._refresh_binding(new_payload),
                str(remaining),
            )
        except Exception:
            raise SessionStateUnavailable("refresh token state unavailable") from None
        if int(result) != 1:
            raise InvalidCredentials("无效或已使用的刷新令牌")
        return IssuedTokenPair(
            new_access,
            new_refresh,
            refresh_expires_in=remaining,
        )

    def read_password_change(self, token: str) -> PasswordChangeClaims:
        payload = self._decode(token)
        required = {"identity_id", "provider_code", "login_name"}
        if payload["token_type"] != PASSWORD_CHANGE_TOKEN_TYPE or not required.issubset(payload):
            raise InvalidCredentials("无效或已使用的改密令牌")
        account_id, identity_id = self._stable_ids(payload)
        if str(payload["provider_code"]) != "local":
            raise InvalidCredentials("无效或已使用的改密令牌")
        return PasswordChangeClaims(
            account_id=account_id,
            identity_id=identity_id,
            provider_code="local",
            login_name=str(payload["login_name"]),
            jti=str(payload["jti"]),
            expires_at=int(payload["exp"]),
        )

    @staticmethod
    def password_change_digest(token: str) -> str:
        """只返回改密令牌的不可逆 SHA-256 指纹，供数据库单次状态绑定。"""

        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def revoke_token(self, token: str) -> None:
        payload = self._decode(token)
        if payload["token_type"] != ACCESS_TOKEN_TYPE:
            raise InvalidCredentials("无效或已吊销的令牌")
        claims = self._claims(payload)
        remaining = max(1, int(payload["exp"]) - int(self.clock().timestamp()))
        try:
            await self.store.set(
                f"auth:jwt:revoked:{claims.jti}",
                "1",
                ex=remaining,
            )
            await self.store.delete(self._refresh_key(claims.session_id))
        except Exception:
            raise SessionStateUnavailable("JWT revocation state unavailable") from None

    async def revoke_user(self, account_id: int) -> None:
        try:
            await self.store.set(
                f"auth:jwt:account-revoked:{account_id}",
                self.clock().timestamp(),
                ex=max(1, int(self.refresh_ttl.total_seconds())),
            )
        except Exception:
            raise SessionStateUnavailable("JWT revocation state unavailable") from None
