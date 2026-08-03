"""应用配置、API Key/回调密钥轮换与 callback SSRF 防护。"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.core.bounded_executor import ExecutorBackpressure, run_bounded
from app.services.crypto import CryptoService, EncryptionContext
from app.services.runtime_policy import (
    InvalidRuntimePolicy,
    parse_private_callback_cidrs,
)

VALID_CATEGORIES = frozenset({"verify", "notice", "market"})
FREQ_OVERRIDE_KEYS = frozenset(
    {"verify_per_minute", "verify_per_day", "market_per_day"}
)
MAX_ALLOWED_IPS = 50


class InvalidAppConfig(ValueError):
    """应用字段或 callback 地址不安全，对应 INVALID_PARAM/422。"""


class AppNotFound(RuntimeError):
    """目标应用不存在。"""


class CallbackValidationUnavailable(RuntimeError):
    """DNS/同步校验执行预算暂时不可用。"""


@dataclass(frozen=True, slots=True)
class AppCreate:
    name: str
    dept: str
    allowed_categories: frozenset[str] = VALID_CATEGORIES
    default_sign: str | None = None
    daily_quota: int = 0
    rate_limit_per_min: int = 60
    blacklist_check: bool = True
    freq_override: Mapping[str, int] | None = None
    allowed_ips: tuple[str, ...] = ()
    callback_url: str | None = None
    callback_report_enabled: bool = False


@dataclass(frozen=True, slots=True)
class AppUpdate:
    dept: str
    allowed_categories: frozenset[str] = VALID_CATEGORIES
    default_sign: str | None = None
    daily_quota: int = 0
    rate_limit_per_min: int = 60
    blacklist_check: bool = True
    freq_override: Mapping[str, int] | None = None
    allowed_ips: tuple[str, ...] = ()
    callback_url: str | None = None
    callback_report_enabled: bool = False
    status: int = 1


class AppRepository(Protocol):
    async def list(self) -> list[dict[str, Any]]: ...

    async def get(self, app_id: int) -> dict[str, Any] | None: ...

    async def create(self, **values: Any) -> int: ...

    async def update(self, app_id: int, **values: Any) -> dict[str, Any]: ...

    async def disable(self, app_id: int, actor: str, ip: str) -> None: ...

    async def rotate_key(self, app_id: int, **values: Any) -> None: ...

    async def revoke_old_key(self, app_id: int, actor: str, ip: str) -> None: ...

    async def rotate_callback_secret(self, app_id: int, **values: Any) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def generate_secret() -> str:
    """生成至少 256 bit 随机性的一次性密钥。"""

    return secrets.token_urlsafe(32)


def _resolve_addresses(hostname: str) -> list[str]:
    records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


@dataclass(frozen=True, slots=True)
class ApprovedCallbackTarget:
    url: str
    hostname: str
    addresses: tuple[str, ...]


class CallbackUrlValidator:
    """保存与出站前复用的 callback URL/DNS/CIDR 校验器。"""

    def __init__(
        self,
        allow_cidrs: str | Sequence[str],
        *,
        resolver: Callable[[str], Sequence[str]] = _resolve_addresses,
        allow_http: bool = False,
    ) -> None:
        raw_cidrs = (
            allow_cidrs.split(",") if isinstance(allow_cidrs, str) else allow_cidrs
        )
        try:
            self.networks = parse_private_callback_cidrs(",".join(raw_cidrs))
        except InvalidRuntimePolicy as exc:
            raise InvalidAppConfig(str(exc)) from exc
        self.resolver = resolver
        self.allow_http = allow_http

    async def validate_for_save(self, url: str) -> str:
        return await self._run(self._validate, url)

    async def validate_for_outbound(self, url: str) -> ApprovedCallbackTarget:
        """出站前必须重新解析 DNS，防止保存后的 DNS rebinding。"""

        validated, hostname, addresses = await self._run(self._validated_parts, url)
        return ApprovedCallbackTarget(validated, hostname, addresses)

    @staticmethod
    async def _run[T](function: Callable[[str], T], url: str) -> T:
        try:
            return await run_bounded(function, url, timeout_s=2)
        except ExecutorBackpressure as error:
            raise CallbackValidationUnavailable("callback DNS 校验繁忙") from error
        except TimeoutError as error:
            raise CallbackValidationUnavailable("callback DNS 校验超时") from error

    def _validate(self, url: str) -> str:
        validated, _hostname, _addresses = self._validated_parts(url)
        return validated

    def _validated_parts(self, url: str) -> tuple[str, str, tuple[str, ...]]:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise InvalidAppConfig("callback URL 格式无效") from exc
        allowed_schemes = {"https", "http"} if self.allow_http else {"https"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            raise InvalidAppConfig(
                "生产 callback URL 仅允许 HTTPS"
                if not self.allow_http
                else "callback URL 仅允许 http/https"
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise InvalidAppConfig("callback URL 禁止凭据或 fragment")
        if port is not None and not 1 <= port <= 65535:
            raise InvalidAppConfig("callback URL 端口无效")
        try:
            addresses = self.resolver(parsed.hostname)
        except OSError as exc:
            raise InvalidAppConfig("callback URL DNS 解析失败") from exc
        if not addresses:
            raise InvalidAppConfig("callback URL DNS 无可用地址")
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise InvalidAppConfig("callback URL DNS 返回无效地址") from exc
            if not any(address in network for network in self.networks):
                raise InvalidAppConfig("callback URL 不在允许的内网 CIDR")
        return url, parsed.hostname, tuple(addresses)


class AppManagementService:
    """保证明文密钥只存在于当前响应内存中，持久层只接收哈希或密文。"""

    def __init__(
        self,
        repository: AppRepository,
        crypto: CryptoService,
        callback_validator: CallbackUrlValidator,
        *,
        secret_generator: Callable[[], str] = generate_secret,
        clock: Callable[[], datetime] = utc_now,
        key_grace: timedelta = timedelta(hours=72),
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.callback_validator = callback_validator
        self.secret_generator = secret_generator
        self.clock = clock
        self.key_grace = key_grace

    @staticmethod
    def _validate(config: AppCreate | AppUpdate) -> None:
        if isinstance(config, AppCreate) and (not config.name or len(config.name) > 64):
            raise InvalidAppConfig("应用名称长度无效")
        if not config.dept or len(config.dept) > 128:
            raise InvalidAppConfig("部门长度无效")
        if not config.allowed_categories or not config.allowed_categories.issubset(
            VALID_CATEGORIES
        ):
            raise InvalidAppConfig("allowed_categories 无效")
        if (
            not 0 <= config.daily_quota <= 100_000_000
            or not 1 <= config.rate_limit_per_min <= 60_000
        ):
            raise InvalidAppConfig("配额或限流参数无效")
        if config.freq_override is not None and (
            not set(config.freq_override).issubset(FREQ_OVERRIDE_KEYS)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in config.freq_override.values()
            )
            or config.freq_override.get("verify_per_minute", 1) > 100
            or config.freq_override.get("verify_per_day", 1) > 10_000
            or config.freq_override.get("market_per_day", 1) > 1_000
        ):
            raise InvalidAppConfig("freq_override 无效")
        if config.callback_report_enabled and not config.callback_url:
            raise InvalidAppConfig("启用明细回调前必须配置 callback_url")
        if isinstance(config, AppUpdate) and config.status not in {0, 1}:
            raise InvalidAppConfig("应用状态无效")

    @staticmethod
    def _key_values(plaintext: str) -> dict[str, str]:
        if len(plaintext) < 16:
            raise InvalidAppConfig("生成的密钥长度不足")
        return {
            "api_key_hash": hashlib.sha256(plaintext.encode()).hexdigest(),
            "api_key_prefix": plaintext[:8],
        }

    @staticmethod
    def _normalize_allowed_ips(entries: Sequence[str]) -> tuple[str, ...]:
        """校验并规范化来源 IP/CIDR 白名单：单 IP 归一化为 /32 或 /128，排序去重。"""

        if len(entries) > MAX_ALLOWED_IPS:
            raise InvalidAppConfig(f"IP 白名单最多 {MAX_ALLOWED_IPS} 条")
        normalized: set[str] = set()
        for raw in entries:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 64:
                raise InvalidAppConfig("IP 白名单条目必须为合法 IP 或 CIDR")
            try:
                network = ipaddress.ip_network(raw.strip(), strict=False)
            except ValueError as exc:
                raise InvalidAppConfig("IP 白名单条目必须为合法 IP 或 CIDR") from exc
            normalized.add(str(network))
        return tuple(sorted(normalized))

    @staticmethod
    def _callback_secret_context(app_name: str) -> EncryptionContext:
        """应用名创建后不可变，可作为 callback secret 的稳定对象标识。"""

        return EncryptionContext(
            domain="callback-secret",
            table="app",
            column="callback_secret_enc",
            object_id=app_name,
        )

    async def _config_values(
        self,
        config: AppCreate | AppUpdate,
    ) -> dict[str, Any]:
        self._validate(config)
        callback_url = (
            await self.callback_validator.validate_for_save(config.callback_url)
            if config.callback_url
            else None
        )
        values: dict[str, Any] = {
            "dept": config.dept,
            "allowed_categories": ",".join(sorted(config.allowed_categories)),
            "default_sign": config.default_sign,
            "daily_quota": config.daily_quota,
            "rate_limit_per_min": config.rate_limit_per_min,
            "blacklist_check": config.blacklist_check,
            "freq_override": dict(config.freq_override) if config.freq_override else None,
            "allowed_ips": self._normalize_allowed_ips(config.allowed_ips),
            "callback_url": callback_url,
            "callback_report_enabled": config.callback_report_enabled,
        }
        if isinstance(config, AppCreate):
            values["name"] = config.name
        else:
            values["status"] = config.status
        return values

    async def list(self) -> list[dict[str, Any]]:
        return await self.repository.list()

    async def get(self, app_id: int) -> dict[str, Any]:
        app = await self.repository.get(app_id)
        if app is None:
            raise AppNotFound("应用不存在")
        return app

    async def create(self, config: AppCreate, *, actor: str, ip: str) -> dict[str, Any]:
        values = await self._config_values(config)
        api_key = self.secret_generator()
        values.update(self._key_values(api_key))
        callback_secret: str | None = None
        if config.callback_url:
            callback_secret = self.secret_generator()
            values["callback_secret_enc"] = self.crypto.encrypt_bound_packed_text(
                callback_secret,
                self._callback_secret_context(config.name),
            )
        else:
            values["callback_secret_enc"] = None
        app_id = await self.repository.create(**values, actor=actor, ip=ip)
        return {"id": app_id, "api_key": api_key, "callback_secret": callback_secret}

    async def update(
        self,
        app_id: int,
        config: AppUpdate,
        *,
        actor: str,
        ip: str,
    ) -> dict[str, Any]:
        values = await self._config_values(config)
        if config.callback_url:
            current = await self.repository.get(app_id)
            if current is None:
                raise AppNotFound("应用不存在")
            if not current.get("callback_secret_configured"):
                raise InvalidAppConfig("请先轮换 callback secret 再配置 callback URL")
        return await self.repository.update(
            app_id,
            **values,
            actor=actor,
            ip=ip,
        )

    async def disable(self, app_id: int, *, actor: str, ip: str) -> None:
        await self.repository.disable(app_id, actor, ip)

    async def rotate_key(self, app_id: int, *, actor: str, ip: str) -> dict[str, Any]:
        api_key = self.secret_generator()
        expires_at = self.clock() + self.key_grace
        await self.repository.rotate_key(
            app_id,
            **self._key_values(api_key),
            old_key_expires_at=expires_at,
            actor=actor,
            ip=ip,
        )
        return {"api_key": api_key, "old_key_expires_at": expires_at}

    async def revoke_old_key(self, app_id: int, *, actor: str, ip: str) -> None:
        await self.repository.revoke_old_key(app_id, actor, ip)

    async def rotate_callback_secret(
        self,
        app_id: int,
        *,
        actor: str,
        ip: str,
    ) -> dict[str, str]:
        callback_secret = self.secret_generator()
        current = await self.repository.get(app_id)
        if current is None:
            raise AppNotFound("应用不存在")
        await self.repository.rotate_callback_secret(
            app_id,
            callback_secret_enc=self.crypto.encrypt_bound_packed_text(
                callback_secret,
                self._callback_secret_context(str(current["name"])),
            ),
            actor=actor,
            ip=ip,
        )
        return {"callback_secret": callback_secret}
