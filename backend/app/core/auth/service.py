"""两种认证后端共享的账号锁定与来源 IP 防爆破逻辑。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from redis.asyncio import Redis

from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
from app.core.auth.identity import normalize_login_name
from app.core.auth.providers import AuthProviderRegistry
from app.core.runtime_resources import redis_client
from app.services.runtime_policy import RuntimePolicy

ACCOUNT_WINDOW_S = 15 * 60
IP_WINDOW_S = 5 * 60


class AccountLocked(RuntimeError):
    """账号已达到失败阈值，对应 ACCOUNT_LOCKED/423。"""


class RateLimited(RuntimeError):
    """来源 IP 已被临时封禁，对应 RATE_LIMITED/429。"""


class AsyncKeyValue(Protocol):
    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: Any, *, ex: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def increment(self, key: str, *, window_s: int) -> int: ...

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...


class RedisKeyValue:
    """Redis 登录状态存储；计数与首次过期设置由 Lua 原子完成。"""

    _INCREMENT_LUA = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return current
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @classmethod
    def from_url(cls, url: str) -> RedisKeyValue:
        return cls(cast(Redis, redis_client(url)))

    async def get(self, key: str) -> Any:
        return await self.redis.get(key)

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        await self.redis.set(key, value, ex=ex)

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)

    async def increment(self, key: str, *, window_s: int) -> int:
        operation = self.redis.eval(self._INCREMENT_LUA, 1, key, str(window_s))
        value = await cast(Awaitable[Any], operation)
        return int(value)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        operation = self.redis.eval(script, numkeys, *args)
        return await cast(Awaitable[Any], operation)


class LoginGuard:
    """认证模式无关的失败计数、账号锁定与 IP 封禁。"""

    def __init__(
        self,
        store: AsyncKeyValue,
        *,
        policy_loader: Callable[[], Awaitable[RuntimePolicy]] | None = None,
    ) -> None:
        self.store = store
        self.policy_loader = policy_loader

    async def _policy(self) -> RuntimePolicy:
        if self.policy_loader is not None:
            return await self.policy_loader()
        return RuntimePolicy.from_mapping({})

    @staticmethod
    def _user_key(kind: str, username: str) -> str:
        return f"auth:{kind}:user:{username.casefold()}"

    @staticmethod
    def _ip_key(kind: str, ip: str) -> str:
        return f"auth:{kind}:ip:{ip}"

    async def check(self, username: str, ip: str) -> None:
        if await self.store.get(self._ip_key("ban", ip)) is not None:
            raise RateLimited("登录来源 IP 已临时封禁")
        if await self.store.get(self._user_key("lock", username)) is not None:
            raise AccountLocked("登录账号已临时锁定")

    async def record_failure(self, username: str, ip: str) -> None:
        policy = await self._policy()
        user_failures = await self.store.increment(
            self._user_key("fail", username),
            window_s=ACCOUNT_WINDOW_S,
        )
        ip_failures = await self.store.increment(
            self._ip_key("fail", ip),
            window_s=IP_WINDOW_S,
        )
        if ip_failures >= policy.login_ip_fail_limit:
            await self.store.set(
                self._ip_key("ban", ip),
                "1",
                ex=policy.login_ip_ban_minutes * 60,
            )
            raise RateLimited("登录来源 IP 已临时封禁")
        if user_failures >= policy.login_fail_limit:
            await self.store.set(
                self._user_key("lock", username),
                "1",
                ex=policy.login_lock_minutes * 60,
            )
            raise AccountLocked("登录账号已临时锁定")

    async def record_success(self, username: str) -> None:
        await self.store.delete(self._user_key("fail", username))
        await self.store.delete(self._user_key("lock", username))


class AuthService:
    """所有 Provider 共享完全相同的登录防护，且锁键不按来源拆分。"""

    def __init__(self, providers: AuthProviderRegistry, guard: LoginGuard) -> None:
        self.providers = providers
        self.guard = guard

    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
    ) -> AuthenticatedIdentity:
        normalized = normalize_login_name(login_name)
        await self.guard.check(normalized, ip)
        try:
            identity = await self.providers.authenticate(
                provider_code,
                normalized,
                password,
            )
        except InvalidCredentials:
            await self.guard.record_failure(normalized, ip)
            raise
        await self.guard.record_success(normalized)
        return identity
