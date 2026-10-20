"""Web 令牌常量、声明投影与签名 keyring 解析。"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.backends import InvalidCredentials
from app.core.auth.roles import Role

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
PASSWORD_CHANGE_TOKEN_TYPE = "password_change"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=7)
PASSWORD_CHANGE_TTL = timedelta(minutes=10)
REFRESH_GRACE_SECONDS = 5
JWT_ISSUER = "sms-platform-web"
JWT_AUDIENCE = "sms-platform-api"
JWT_KEY_VERSION_BYTES = 2
REFRESH_TAB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SessionMode = Literal["refresh", "access_only"]
SESSION_MODES: frozenset[str] = frozenset({"refresh", "access_only"})


def _read_session_mode(payload: dict[str, Any]) -> SessionMode:
    """旧令牌缺省 refresh；未知取值失败关闭，禁止静默升级。"""

    raw = payload.get("session_mode", "refresh")
    if raw not in SESSION_MODES:
        raise InvalidCredentials("无效或已吊销的令牌")
    return cast(SessionMode, raw)


class ReauthenticationRequired(RuntimeError):
    """外部目录 refresh family 已达到完整重新认证期限。"""


MAX_VALID_ACCESS_LIFETIME_MS = max(1, int(ACCESS_TOKEN_TTL.total_seconds() * 1000))


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
    auth_time: float | None
    reauth_deadline: float | None
    auth_policy_version: int | None
    session_mode: SessionMode

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
        auth_time: float | None = None,
        reauth_deadline: float | None = None,
        auth_policy_version: int | None = None,
        session_mode: SessionMode = "refresh",
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
        object.__setattr__(self, "auth_time", auth_time)
        object.__setattr__(self, "reauth_deadline", reauth_deadline)
        object.__setattr__(self, "auth_policy_version", auth_policy_version)
        object.__setattr__(
            self,
            "session_mode",
            session_mode if session_mode in SESSION_MODES else "refresh",
        )

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
        key = secret.encode("utf-8")
        if not secret.strip() or len(key) < 32:
            raise ValueError("JWT key must contain at least 32 bytes of key material")
        return JwtKeyring(1, {1: key})
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


def max_valid_access_lifetime_ms() -> int:
    """验证协议允许的已签发 Access 寿命上限，不因实例下调 TTL 而缩短。"""

    return MAX_VALID_ACCESS_LIFETIME_MS
