"""应用 API Key 候选查询、双 Key 宽限校验与请求上下文注入。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from starlette.requests import Request

from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings

PREFIX_LENGTH = 8
MIN_KEY_LENGTH = 16
MAX_ALLOWED_IPS = 50
VALID_CATEGORIES = frozenset({"verify", "notice", "market"})
api_key_scheme = APIKeyHeader(
    name="X-Api-Key",
    scheme_name="ApiKeyAuth",
    auto_error=False,
)


class InvalidApiKey(RuntimeError):
    """统一无效 Key 错误，不泄露应用或轮换状态。"""


@dataclass(frozen=True, slots=True)
class ApiKeyCandidate:
    app_id: int
    name: str
    dept: str
    allowed_categories: str
    current_hash: str
    previous_hash: str | None
    previous_expires_at: datetime | None
    default_sign: str | None = None
    daily_quota: int = 0
    blacklist_check: bool = True
    freq_override: dict[str, int] | None = None
    rate_limit_per_min: int = 60
    allowed_ips: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApiAppContext:
    app_id: int
    name: str
    dept: str
    allowed_categories: frozenset[str]
    default_sign: str | None = None
    daily_quota: int = 0
    blacklist_check: bool = True
    freq_override: dict[str, int] | None = None
    rate_limit_per_min: int = 60
    allowed_ips: tuple[str, ...] = ()


class ApiKeyRepository(Protocol):
    async def find_candidates(self, prefix: str) -> list[ApiKeyCandidate]: ...


class ApiKeyVerifier(Protocol):
    async def authenticate(self, key: str) -> ApiAppContext: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class SqlApiKeyRepository:
    """仅按非敏感前缀缩小候选，哈希比较始终留在应用层。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url_for("auth"))

    async def find_candidates(self, prefix: str) -> list[ApiKeyCandidate]:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                text(
                    """
                        SELECT id, name, dept, allowed_categories, default_sign,
                               daily_quota, blacklist_check, freq_override,
                               rate_limit_per_min,
                               allowed_ips,
                               api_key_hash, api_key_prev_hash, api_key_prev_expires
                        FROM app
                        WHERE status = 1
                          AND (api_key_prefix = :prefix OR api_key_prev_prefix = :prefix)
                        """
                ),
                {"prefix": prefix},
            )
            return [
                ApiKeyCandidate(
                    app_id=int(row["id"]),
                    name=str(row["name"]),
                    dept=str(row["dept"]),
                    allowed_categories=str(row["allowed_categories"]),
                    current_hash=str(row["api_key_hash"]),
                    previous_hash=(
                        str(row["api_key_prev_hash"])
                        if row["api_key_prev_hash"] is not None
                        else None
                    ),
                    previous_expires_at=cast(
                        datetime | None,
                        row["api_key_prev_expires"],
                    ),
                    default_sign=(
                        str(row["default_sign"]) if row["default_sign"] is not None else None
                    ),
                    daily_quota=int(row["daily_quota"]),
                    blacklist_check=bool(row["blacklist_check"]),
                    freq_override=cast(dict[str, int] | None, row["freq_override"]),
                    rate_limit_per_min=int(row.get("rate_limit_per_min", 60)),
                    allowed_ips=tuple(
                        str(item) for item in (row["allowed_ips"] or ())
                    ),
                )
                for row in result.mappings()
            ]


@lru_cache(maxsize=256)
def _compile_allowlist(
    entries: tuple[str, ...],
) -> frozenset[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """预编译规范化 CIDR 白名单；配置损坏时 fail closed。"""

    networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for raw in entries:
        try:
            networks.add(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise ApiError(500, "INTERNAL_ERROR", "应用 IP 白名单配置无效", None) from exc
    return frozenset(networks)


def _ip_allowed(client_host: str, allowed_ips: tuple[str, ...]) -> bool:
    """空白名单放行；非空白名单必须命中且客户端 IP 可解析（fail closed）。"""

    if not allowed_ips:
        return True
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    networks = _compile_allowlist(allowed_ips)
    return any(client_ip in network for network in networks)


def _enforce_ip_allowlist(request: Request, context: ApiAppContext) -> None:
    client_host = trusted_client_ip(request)
    if not _ip_allowed(client_host, context.allowed_ips):
        raise ApiError(403, "IP_NOT_ALLOWED", "来源 IP 不在应用白名单", None)


class ApiKeyAuthenticator:
    """以常量时间哈希比较验证当前 Key 或仍在宽限期的旧 Key。"""

    def __init__(
        self,
        repository: ApiKeyRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.clock = clock

    async def authenticate(self, key: str) -> ApiAppContext:
        if len(key) < MIN_KEY_LENGTH:
            raise InvalidApiKey("API Key 无效")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("API Key clock must return timezone-aware datetime")
        candidates = await self.repository.find_candidates(key[:PREFIX_LENGTH])
        for candidate in candidates:
            current_matches = hmac.compare_digest(digest, candidate.current_hash)
            previous_matches = (
                candidate.previous_hash is not None
                and candidate.previous_expires_at is not None
                and candidate.previous_expires_at > now
                and hmac.compare_digest(digest, candidate.previous_hash)
            )
            if current_matches or previous_matches:
                categories = frozenset(
                    item.strip() for item in candidate.allowed_categories.split(",") if item.strip()
                )
                if not categories or not categories.issubset(VALID_CATEGORIES):
                    raise InvalidApiKey("API Key 无效")
                return ApiAppContext(
                    candidate.app_id,
                    candidate.name,
                    candidate.dept,
                    categories,
                    candidate.default_sign,
                    candidate.daily_quota,
                    candidate.blacklist_check,
                    candidate.freq_override,
                    candidate.rate_limit_per_min,
                    candidate.allowed_ips,
                )
        raise InvalidApiKey("API Key 无效")


@lru_cache
def get_api_key_authenticator() -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(SqlApiKeyRepository())


async def optional_api_app(
    request: Request,
    key: Annotated[str | None, Security(api_key_scheme)],
    authenticator: Annotated[ApiKeyVerifier, Depends(get_api_key_authenticator)],
) -> ApiAppContext | None:
    """仅在路由显式声明后解析 API Key；缺少 Key 时交由 Bearer 依赖处理。"""

    existing = getattr(request.state, "sms_app", None)
    if isinstance(existing, ApiAppContext):
        _enforce_ip_allowlist(request, existing)
        return existing
    if not key:
        return None
    try:
        context = await authenticator.authenticate(key)
    except InvalidApiKey:
        raise _unauthorized() from None
    _enforce_ip_allowlist(request, context)
    request.state.sms_app = context
    return context


async def require_api_app(
    request: Request,
    key: Annotated[str | None, Security(api_key_scheme)],
    authenticator: Annotated[ApiKeyVerifier, Depends(get_api_key_authenticator)],
) -> ApiAppContext:
    """显式保护纯 API client 路由，认证依赖不可由路径或 Header 组合推断。"""

    existing = getattr(request.state, "sms_app", None)
    if isinstance(existing, ApiAppContext):
        _enforce_ip_allowlist(request, existing)
        return existing
    if not key:
        raise _unauthorized()
    try:
        context = await authenticator.authenticate(key)
    except InvalidApiKey:
        raise _unauthorized() from None
    _enforce_ip_allowlist(request, context)
    request.state.sms_app = context
    return context


def _unauthorized() -> ApiError:
    return ApiError(401, "UNAUTHORIZED", "API Key 无效", None)
