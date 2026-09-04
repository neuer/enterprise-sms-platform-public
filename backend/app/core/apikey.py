"""应用 API Key 候选查询、双 Key 宽限校验与请求上下文注入。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from starlette.requests import Request

from app.core.auth.accounts import ApplicationPrincipal
from app.core.auth.principal_context import bind_audit_principal
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.runtime_resources import database_engine
from app.services.crypto import ParsedKeyring, parse_secret_keyring
from app.settings import Settings, get_settings

PREFIX_LENGTH = 8
MIN_KEY_LENGTH = 16
MAX_ALLOWED_IPS = 50
VALID_CATEGORIES = frozenset({"verify", "notice", "market"})
LOGGER = logging.getLogger(__name__)


class UnknownPepperVersionError(RuntimeError):
    """记录绑定的 pepper 版本不在当前独立 keyring 中；必须失败关闭。"""

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__("api key pepper version is unavailable")


def _legacy_key_digest(key: str) -> str:
    # API Key 是高熵随机令牌，不是口令；SHA-256 仅作等长摘要比对。
    return hashlib.sha256(key.encode("utf-8")).hexdigest()  # codeql[py/weak-sensitive-data-hashing]


def load_api_key_pepper_keyring(settings: Settings | None = None) -> ParsedKeyring | None:
    """读取独立 API Key pepper keyring；缺文件时由调用方决定是否回退。"""

    try:
        raw = (settings or get_settings()).credential("api_key_pepper_key")
    except Exception:
        return None
    if not raw:
        return None
    return parse_secret_keyring(raw, label="API Key pepper")


def require_api_key_pepper_keyring(settings: Settings | None = None) -> ParsedKeyring:
    """就绪检查必须能解析独立 pepper；不得回退到 data_hmac_key。"""

    ring = load_api_key_pepper_keyring(settings)
    if ring is None:
        raise RuntimeError("api key pepper keyring is unavailable")
    return ring


def issue_api_key_digest(
    key: str, *, settings: Settings | None = None
) -> tuple[str, int | None]:
    """新摘要绑定独立 pepper 的 active_version；无 pepper 时写 legacy SHA-256。"""

    ring = load_api_key_pepper_keyring(settings)
    if ring is None:
        return _legacy_key_digest(key), None
    return _peppered_digest(key, ring.keys[ring.active_version]), ring.active_version


def hash_api_key(key: str, *, settings: Settings | None = None) -> str:
    """新写入使用 HMAC-SHA256(pepper, key)；无独立 pepper 时回退 SHA-256。"""

    return issue_api_key_digest(key, settings=settings)[0]


def _peppered_digest(key: str, pepper: bytes) -> str:
    # HMAC-SHA256(pepper, key) 用于高熵 API Key，不是口令 KDF。
    return hmac.new(  # codeql[py/weak-sensitive-data-hashing]
        pepper, key.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _digest_matches(
    key: str,
    stored: str,
    version: int | None = None,
    *,
    settings: Settings | None = None,
) -> bool:
    if not stored:
        return False
    if version is None:
        try:
            return hmac.compare_digest(_legacy_key_digest(key), stored)
        except ValueError:
            return False
    try:
        ring = load_api_key_pepper_keyring(settings)
    except ValueError as exc:
        raise UnknownPepperVersionError(version) from exc
    if ring is None or version not in ring.keys:
        raise UnknownPepperVersionError(version)
    try:
        return hmac.compare_digest(_peppered_digest(key, ring.keys[version]), stored)
    except ValueError:
        return False


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
    current_hash_version: int | None = None
    previous_hash_version: int | None = None
    default_sign: str | None = None
    daily_quota: int = 0
    blacklist_check: bool = True
    freq_override: dict[str, int] | None = None
    rate_limit_per_min: int = 60
    allowed_ips: tuple[str, ...] = ()
    recipient_limit_per_min: int = 10_000
    segment_limit_per_min: int = 10_000
    max_in_flight_chunks: int = 200
    allow_market_api_bulk: bool = False
    ip_allowlist_exempt_until: datetime | None = None


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
    recipient_limit_per_min: int = 10_000
    segment_limit_per_min: int = 10_000
    max_in_flight_chunks: int = 200
    allow_market_api_bulk: bool = False
    ip_allowlist_exempt_until: datetime | None = None


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
                               rate_limit_per_min, recipient_limit_per_min,
                               segment_limit_per_min, max_in_flight_chunks,
                               allow_market_api_bulk, allowed_ips,
                               ip_allowlist_exempt_until,
                               api_key_hash, api_key_prev_hash, api_key_prev_expires,
                               api_key_hash_version, api_key_prev_hash_version
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
                    current_hash_version=(
                        int(row["api_key_hash_version"])
                        if row["api_key_hash_version"] is not None
                        else None
                    ),
                    previous_hash_version=(
                        int(row["api_key_prev_hash_version"])
                        if row["api_key_prev_hash_version"] is not None
                        else None
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
                    recipient_limit_per_min=int(
                        row.get("recipient_limit_per_min") or 10_000
                    ),
                    segment_limit_per_min=int(
                        row.get("segment_limit_per_min") or 10_000
                    ),
                    max_in_flight_chunks=int(row.get("max_in_flight_chunks") or 200),
                    allow_market_api_bulk=bool(row.get("allow_market_api_bulk") or False),
                    ip_allowlist_exempt_until=cast(
                        datetime | None,
                        row.get("ip_allowlist_exempt_until"),
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


def _empty_allowlist_permitted(context: ApiAppContext) -> bool:
    """生产空白名单仅在未过期豁免内放行；开发/测试仍允许空名单。"""

    if get_settings().environment != "production":
        return True
    until = context.ip_allowlist_exempt_until
    return until is not None and until.tzinfo is not None and until > datetime.now(UTC)


def _enforce_ip_allowlist(request: Request, context: ApiAppContext) -> None:
    client_host = trusted_client_ip(request)
    if not context.allowed_ips:
        if not _empty_allowlist_permitted(context):
            raise ApiError(403, "IP_NOT_ALLOWED", "来源 IP 不在应用白名单", None)
        LOGGER.warning(
            "api key accepted with empty ip allowlist",
            extra={"app_id": context.app_id},
        )
    if not _ip_allowed(client_host, context.allowed_ips):
        raise ApiError(403, "IP_NOT_ALLOWED", "来源 IP 不在应用白名单", None)


def _bind_app_principal(context: ApiAppContext) -> None:
    bind_audit_principal(ApplicationPrincipal(context.app_id, context.name, context.dept))


class ApiKeyAuthenticator:
    """以常量时间哈希比较验证当前 Key 或仍在宽限期的旧 Key。"""

    def __init__(
        self,
        repository: ApiKeyRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock
        self.settings = settings

    async def authenticate(self, key: str) -> ApiAppContext:
        dummy = "0" * 64
        if len(key) < MIN_KEY_LENGTH:
            hmac.compare_digest(_legacy_key_digest(key), dummy)
            raise InvalidApiKey("API Key 无效")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("API Key clock must return timezone-aware datetime")
        candidates = await self.repository.find_candidates(key[:PREFIX_LENGTH])
        for candidate in candidates:
            try:
                current_matches = _digest_matches(
                    key,
                    candidate.current_hash,
                    candidate.current_hash_version,
                    settings=self.settings,
                )
                previous_matches = (
                    candidate.previous_hash is not None
                    and candidate.previous_expires_at is not None
                    and candidate.previous_expires_at > now
                    and _digest_matches(
                        key,
                        candidate.previous_hash,
                        candidate.previous_hash_version,
                        settings=self.settings,
                    )
                )
            except UnknownPepperVersionError:
                LOGGER.warning("api key pepper version unavailable")
                raise InvalidApiKey("API Key 无效") from None
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
                    candidate.recipient_limit_per_min,
                    candidate.segment_limit_per_min,
                    candidate.max_in_flight_chunks,
                    candidate.allow_market_api_bulk,
                    candidate.ip_allowlist_exempt_until,
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
        _bind_app_principal(existing)
        return existing
    if not key:
        return None
    try:
        context = await authenticator.authenticate(key)
    except InvalidApiKey:
        return None
    _enforce_ip_allowlist(request, context)
    request.state.sms_app = context
    _bind_app_principal(context)
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
        _bind_app_principal(existing)
        return existing
    if not key:
        raise _unauthorized()
    try:
        context = await authenticator.authenticate(key)
    except InvalidApiKey:
        raise _unauthorized() from None
    _enforce_ip_allowlist(request, context)
    request.state.sms_app = context
    _bind_app_principal(context)
    return context


def _unauthorized() -> ApiError:
    return ApiError(401, "UNAUTHORIZED", "API Key 无效", None)
