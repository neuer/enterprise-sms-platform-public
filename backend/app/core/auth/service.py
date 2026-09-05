"""两种认证后端共享的可恢复失败阈值与来源 IP 防爆破逻辑。"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Protocol, cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.auth.backends import (
    AuthenticatedIdentity,
    AuthenticationPurpose,
    InvalidCredentials,
    ProviderCapacityUnavailable,
    SessionStateUnavailable,
)
from app.core.auth.guard_policy import AuthGuardPolicy
from app.core.auth.identity import normalize_login_name
from app.core.auth.observability import (
    TransitionErrorClass,
    TransitionOwner,
    observe_admit,
    observe_transition_claim,
    observe_transition_database_duration,
    observe_transition_dead,
    observe_transition_retry,
)
from app.core.auth.providers import AuthProviderRegistry
from app.core.auth.security_events import (
    AuthSecurityEventWriter,
    AuthSecurityTransition,
)
from app.core.runtime_resources import redis_client

ACCOUNT_WINDOW_S = 15 * 60
IP_WINDOW_S = 5 * 60
PROVIDER_CAPACITY_BACKOFF_S = 2
PREHASH_BURST_CAPACITY = 5
PREHASH_REFILL_MS = 15_000
PREHASH_WINDOW_LIMIT = 20
PREHASH_WINDOW_S = 5 * 60
LEASE_SAFETY_MARGIN_S = 5.0
AUDIT_DUE_KEY = "auth:audit:due"
AUDIT_RECOVERY_TTL_S = 24 * 60 * 60
MAX_TRANSITION_ATTEMPTS = 20
MAX_TRANSITION_RECOVERY_MS = AUDIT_RECOVERY_TTL_S * 1000
_SAFE_PROVIDER_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
LOGGER = logging.getLogger(__name__)


def writer_lease_ms(settings: Any | None = None) -> int:
    """Writer Lease 必须覆盖 pool + connect + statement 最坏耗时。"""

    pool_s = 3.0
    connect_s = 3.0
    statement_ms = 15_000
    if settings is not None:
        pool_s = float(settings.db_pool_timeout_seconds)
        connect_s = float(settings.db_connect_timeout_seconds)
        statement_ms = int(settings.db_api_statement_timeout_ms)
    seconds = pool_s + connect_s + statement_ms / 1000.0 + LEASE_SAFETY_MARGIN_S
    return max(1, int(seconds * 1000))


WRITER_LEASE_MS = writer_lease_ms()


def _error_class(error: BaseException) -> TransitionErrorClass:
    name = type(error).__name__.casefold()
    if "timeout" in name:
        return "timeout"
    return "unavailable"


_CLAIM_LUA = """
local function claim_write(audit_key, action, lease_id, now_ms, lease_ms,
  recovery_ttl_s, transition_id)
  if recovery_ttl_s < 1 then recovery_ttl_s = 1 end
  local state = redis.call('HGET', audit_key, 'state')
  if state == 'audited' or state == 'dead' then
    return '', state
  end
  if state == 'writing' then
    local expires = tonumber(redis.call('HGET', audit_key, 'lease_expires_ms') or '0')
    if expires > now_ms then
      return '', 'writing'
    end
  end
  if state == 'pending' then
    local retry_at = tonumber(redis.call('HGET', audit_key, 'next_retry_at_ms') or '0')
    if retry_at > now_ms then
      return '', 'pending'
    end
  end
  redis.call('HSET', audit_key, 'state', 'writing', 'lease_id', lease_id,
    'lease_expires_ms', now_ms + lease_ms, 'action', action)
  redis.call('HSETNX', audit_key, 'created_at_ms', tostring(now_ms))
  redis.call('EXPIRE', audit_key, recovery_ttl_s)
  redis.call('ZADD', 'auth:audit:due', now_ms + lease_ms, transition_id)
  return lease_id, 'writing'
end
"""


class AccountLocked(RuntimeError):
    """错误凭据已达到账号失败阈值，对应 ACCOUNT_LOCKED/423。"""


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
        try:
            return await self.redis.get(key)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        try:
            await self.redis.set(key, value, ex=ex)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error

    async def delete(self, key: str) -> None:
        try:
            await self.redis.delete(key)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error

    async def increment(self, key: str, *, window_s: int) -> int:
        try:
            operation = self.redis.eval(self._INCREMENT_LUA, 1, key, str(window_s))
            value = await cast(Awaitable[Any], operation)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error
        return int(value)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        try:
            operation = self.redis.eval(script, numkeys, *args)
            return await cast(Awaitable[Any], operation)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error


class LoginGuard:
    """认证模式无关的失败计数、可恢复阈值标记与 IP 封禁。"""

    _ACK_LUA = """
    -- auth-audit-ack-v1
    local lease = redis.call('HGET', KEYS[1], 'lease_id')
    if lease and lease == ARGV[1] then
      redis.call('HSET', KEYS[1], 'state', 'audited')
      redis.call('HDEL', KEYS[1], 'lease_id', 'lease_expires_ms', 'next_retry_at_ms')
      local prefix = 'auth:audit:transition:'
      redis.call('ZREM', 'auth:audit:due', string.sub(KEYS[1], #prefix + 1))
      local ttl = tonumber(ARGV[2])
      if ttl and ttl > 0 then redis.call('EXPIRE', KEYS[1], ttl) end
      return 1
    end
    return 0
    """

    _FAIL_LUA = """
    -- auth-audit-fail-v1
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local lease = redis.call('HGET', KEYS[1], 'lease_id')
    if lease and lease == ARGV[1] then
      local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or '0') + 1
      local created = tonumber(redis.call('HGET', KEYS[1], 'created_at_ms') or now_ms)
      local delays = {1000, 2000, 5000, 10000, 30000}
      local delay = delays[math.min(attempts, #delays)]
      local prefix = 'auth:audit:transition:'
      local transition_id = string.sub(KEYS[1], #prefix + 1)
      local recovery_ttl = tonumber(ARGV[2])
      if recovery_ttl < 1 then recovery_ttl = 1 end
      if attempts >= tonumber(ARGV[3]) or (now_ms - created) >= tonumber(ARGV[4]) then
        redis.call('HSET', KEYS[1], 'state', 'dead', 'attempts', tostring(attempts))
        redis.call('HDEL', KEYS[1], 'lease_id', 'lease_expires_ms', 'next_retry_at_ms')
        redis.call('ZREM', 'auth:audit:due', transition_id)
        redis.call('EXPIRE', KEYS[1], recovery_ttl)
        return -1
      end
      redis.call('HSET', KEYS[1], 'state', 'pending', 'attempts', tostring(attempts),
        'next_retry_at_ms', now_ms + delay)
      redis.call('HDEL', KEYS[1], 'lease_id', 'lease_expires_ms')
      redis.call('ZADD', 'auth:audit:due', now_ms + delay, transition_id)
      redis.call('EXPIRE', KEYS[1], recovery_ttl)
      return attempts
    end
    return 0
    """

    _BIND_LUA = """
    -- auth-audit-bind-v1
    if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
    redis.call('HSETNX', KEYS[1], 'provider_code', ARGV[1])
    redis.call('HSETNX', KEYS[1], 'result_code', ARGV[2])
    redis.call('HSETNX', KEYS[1], 'count', ARGV[3])
    redis.call('HSETNX', KEYS[1], 'remaining_ttl_seconds', ARGV[4])
    redis.call('HSETNX', KEYS[1], 'ip', ARGV[5])
    redis.call('HSETNX', KEYS[1], 'action', ARGV[6])
    return 1
    """

    _SCAN_DUE_LUA = """
    -- auth-audit-due-scan-v1
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    return redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now_ms, 'LIMIT', 0, tonumber(ARGV[1]))
    """

    _RECONCILE_CLAIM_LUA = _CLAIM_LUA + """
    -- auth-audit-reconcile-claim-v1
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local transition_id = ARGV[1]
    local audit_key = 'auth:audit:transition:' .. transition_id
    local action = redis.call('HGET', audit_key, 'action') or ''
    local previous = redis.call('HGET', audit_key, 'state') or ''
    local lease, state = claim_write(
      audit_key, action, ARGV[2], now_ms, tonumber(ARGV[3]), tonumber(ARGV[4]),
      transition_id
    )
    return {
      lease, state, action,
      redis.call('HGET', audit_key, 'provider_code') or '',
      redis.call('HGET', audit_key, 'result_code') or '',
      redis.call('HGET', audit_key, 'count') or '0',
      redis.call('HGET', audit_key, 'remaining_ttl_seconds') or '0',
      redis.call('HGET', audit_key, 'ip') or '',
      previous
    }
    """

    _DUE_STATS_LUA = """
    -- auth-audit-due-stats-v1
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local count = tonumber(redis.call('ZCARD', KEYS[1])) or 0
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    if count == 0 or oldest == nil or #oldest < 2 then
      return {count, 0}
    end
    local age_ms = now_ms - tonumber(oldest[2])
    if age_ms < 0 then age_ms = 0 end
    return {count, math.floor(age_ms / 1000)}
    """

    _ADMIT_LUA = _CLAIM_LUA + """
    -- auth-admit-v2
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local ban = redis.call('GET', KEYS[1])
    if ban then
      if ban == '1' then
        redis.call('SET', KEYS[1], ARGV[1], 'XX', 'KEEPTTL')
        ban = ARGV[1]
      end
      local ttl = tonumber(redis.call('TTL', KEYS[1])) or 0
      local lease, state = claim_write(
        'auth:audit:transition:' .. ban, 'auth_ip_banned', ARGV[7], now_ms,
        tonumber(ARGV[8]), tonumber(ARGV[9]), ban
      )
      return {
        2, '', 0, ban, tonumber(redis.call('GET', KEYS[2]) or '0'),
        0, ttl, '', lease, '', state
      }
    end

    -- 用户名失败阈值只在凭据校验失败后评估。若在这里按用户名拒绝，
    -- 远程攻击者只需知道登录名即可阻断正确凭据。

    local long_count = tonumber(redis.call('GET', KEYS[4]) or '0')
    if long_count >= tonumber(ARGV[5]) then
      return {3, '', 0, '', long_count, 0, redis.call('TTL', KEYS[4]), '', '', '', ''}
    end

    local state = redis.call('HMGET', KEYS[3], 'tokens', 'updated_ms')
    local tokens = tonumber(state[1]) or tonumber(ARGV[2])
    local updated_ms = tonumber(state[2]) or now_ms
    if now_ms > updated_ms then
      tokens = math.min(
        tonumber(ARGV[2]),
        tokens + ((now_ms - updated_ms) / tonumber(ARGV[3]))
      )
    end
    if tokens < 1 then
      redis.call('HSET', KEYS[3], 'tokens', tokens, 'updated_ms', now_ms)
      redis.call('PEXPIRE', KEYS[3], ARGV[4])
      return {3, '', 0, '', long_count, 0, redis.call('PTTL', KEYS[3]), '', '', '', ''}
    end
    tokens = tokens - 1
    redis.call('HSET', KEYS[3], 'tokens', tokens, 'updated_ms', now_ms)
    redis.call('PEXPIRE', KEYS[3], ARGV[4])
    long_count = redis.call('INCR', KEYS[4])
    if long_count == 1 then redis.call('EXPIRE', KEYS[4], ARGV[6]) end
    return {0, '', 0, '', long_count, 0, redis.call('TTL', KEYS[4]), '', '', '', ''}
    """

    _INVALID_LUA = _CLAIM_LUA + """
    -- auth-invalid-v1
    local now = redis.call('TIME')
    local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local existing_ban = redis.call('GET', KEYS[4])
    if existing_ban then
      if existing_ban == '1' then
        redis.call('SET', KEYS[4], ARGV[8], 'XX', 'KEEPTTL')
        existing_ban = ARGV[8]
      end
      local ttl = tonumber(redis.call('TTL', KEYS[4])) or 0
      local lease, state = claim_write(
        'auth:audit:transition:' .. existing_ban, 'auth_ip_banned', ARGV[9],
        now_ms, tonumber(ARGV[10]), tonumber(ARGV[12]), existing_ban
      )
      return {2, '', 0, existing_ban, 0, 0, ttl, '', lease, '', state}
    end

    local user_count = redis.call('INCR', KEYS[1])
    if user_count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    local ip_count = redis.call('INCR', KEYS[2])
    if ip_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end

    local lock = redis.call('GET', KEYS[3])
    if lock == '1' then
      redis.call('SET', KEYS[3], ARGV[7], 'XX', 'KEEPTTL')
      lock = ARGV[7]
    end
    if user_count >= tonumber(ARGV[3]) and not lock then
      redis.call('SET', KEYS[3], ARGV[7], 'NX', 'EX', ARGV[4])
      lock = redis.call('GET', KEYS[3])
    end
    local ban = redis.call('GET', KEYS[4])
    if ip_count >= tonumber(ARGV[5]) and not ban then
      redis.call('SET', KEYS[4], ARGV[8], 'NX', 'EX', ARGV[6])
      ban = redis.call('GET', KEYS[4])
    end
    local lock_lease, lock_state, ban_lease, ban_state = '', '', '', ''
    if lock and lock ~= '' then
      lock_lease, lock_state = claim_write(
        'auth:audit:transition:' .. lock, 'auth_account_locked', ARGV[9],
        now_ms, tonumber(ARGV[10]), tonumber(ARGV[12]), lock
      )
    end
    if ban and ban ~= '' then
      ban_lease, ban_state = claim_write(
        'auth:audit:transition:' .. ban, 'auth_ip_banned', ARGV[11],
        now_ms, tonumber(ARGV[10]), tonumber(ARGV[12]), ban
      )
    end
    if ban then
      return {
        2, lock or '', user_count, ban, ip_count,
        lock and redis.call('TTL', KEYS[3]) or 0,
        redis.call('TTL', KEYS[4]),
        lock_lease, ban_lease, lock_state, ban_state
      }
    end
    if lock then
      return {
        1, lock, user_count, '', ip_count,
        redis.call('TTL', KEYS[3]), 0,
        lock_lease, '', lock_state, ''
      }
    end
    return {0, '', user_count, '', ip_count, 0, 0, '', '', '', ''}
    """

    def __init__(
        self,
        store: AsyncKeyValue,
        *,
        policy_loader: Callable[[], Awaitable[AuthGuardPolicy]] | None = None,
        security_events: AuthSecurityEventWriter | None = None,
        lease_ms: int | None = None,
        owner: TransitionOwner = "api",
    ) -> None:
        self.store = store
        self.policy_loader = policy_loader
        self.security_events = security_events
        self.lease_ms = writer_lease_ms() if lease_ms is None else lease_ms
        self.owner = owner

    async def snapshot(self) -> AuthGuardPolicy:
        """仅在凭据失败后取得阈值快照；准入路径不得调用。"""

        if self.policy_loader is not None:
            return await self.policy_loader()
        return AuthGuardPolicy.defaults()

    @staticmethod
    def _user_key(kind: str, username: str) -> str:
        return f"auth:{kind}:user:{username.casefold()}"

    @staticmethod
    def _ip_key(kind: str, ip: str) -> str:
        return f"auth:{kind}:ip:{ip}"

    @staticmethod
    def _prehash_key(kind: str, ip: str) -> str:
        return f"auth:prehash:{kind}:ip:{ip}"

    async def admit(self, provider_code: str, username: str, ip: str) -> None:
        """在读取密码摘要或连接目录前原子执行封禁与 IP 准入。"""

        del username
        result = await self.store.eval(
            self._ADMIT_LUA,
            4,
            self._ip_key("ban", ip),
            self._ip_key("fail", ip),
            self._prehash_key("bucket", ip),
            self._prehash_key("window", ip),
            str(uuid4()),
            str(PREHASH_BURST_CAPACITY),
            str(PREHASH_REFILL_MS),
            str(PREHASH_WINDOW_S * 1000),
            str(PREHASH_WINDOW_LIMIT),
            str(PREHASH_WINDOW_S),
            str(uuid4()),
            str(self.lease_ms),
            str(AUDIT_RECOVERY_TTL_S),
        )
        await self._enforce_result(result, provider_code=provider_code, ip=ip)

    async def check_provider_capacity(self, provider_code: str) -> None:
        """在调度同步 Provider 工作前拒绝仍处于容量退避期的请求。"""

        if await self.store.get(self._provider_capacity_key(provider_code)) is not None:
            raise ProviderCapacityUnavailable("认证源容量暂不可用")

    @staticmethod
    def _provider_capacity_key(provider_code: str) -> str:
        return f"auth:capacity:provider:{provider_code.casefold()}"

    async def record_capacity_failure(self, provider_code: str) -> None:
        """外部 Provider 容量故障只触发短退避，不伪装成凭据爆破。"""

        await self.store.set(
            self._provider_capacity_key(provider_code),
            "1",
            ex=PROVIDER_CAPACITY_BACKOFF_S,
        )

    async def record_failure(
        self,
        username: str,
        ip: str,
        provider_code: str = "unknown",
        *,
        policy: AuthGuardPolicy | None = None,
    ) -> None:
        snapshot = policy or await self.snapshot()
        result = await self.store.eval(
            self._INVALID_LUA,
            4,
            self._user_key("fail", username),
            self._ip_key("fail", ip),
            self._user_key("lock", username),
            self._ip_key("ban", ip),
            str(ACCOUNT_WINDOW_S),
            str(IP_WINDOW_S),
            str(snapshot.login_fail_limit),
            str(snapshot.login_lock_minutes * 60),
            str(snapshot.login_ip_fail_limit),
            str(snapshot.login_ip_ban_minutes * 60),
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
            str(self.lease_ms),
            str(uuid4()),
            str(AUDIT_RECOVERY_TTL_S),
        )
        await self._enforce_result(result, provider_code=provider_code, ip=ip)

    async def _enforce_result(
        self,
        raw: Any,
        *,
        provider_code: str,
        ip: str,
    ) -> None:
        parsed = self._parse_guard_result(raw)
        code, lock_transition, user_count, ban_transition, ip_count, lock_ttl, ban_ttl = parsed[:7]
        lock_lease, ban_lease, lock_state, ban_state = parsed[7:]
        normalized_provider = provider_code.casefold()
        safe_provider = (
            normalized_provider
            if _SAFE_PROVIDER_CODE.fullmatch(normalized_provider) is not None
            else "invalid"
        )
        errors: list[BaseException] = []
        if lock_transition:
            error = await self._settle_transition(
                action="auth_account_locked",
                transition_id=lock_transition,
                lease_id=lock_lease,
                audit_state=lock_state,
                provider_code=safe_provider,
                result_code="ACCOUNT_LOCKED",
                count=max(1, user_count),
                remaining_ttl_seconds=max(1, lock_ttl),
                ip=ip,
            )
            if error is not None:
                errors.append(error)
        if ban_transition:
            error = await self._settle_transition(
                action="auth_ip_banned",
                transition_id=ban_transition,
                lease_id=ban_lease,
                audit_state=ban_state,
                provider_code=safe_provider,
                result_code="RATE_LIMITED",
                count=max(1, ip_count),
                remaining_ttl_seconds=max(1, ban_ttl),
                ip=ip,
            )
            if error is not None:
                errors.append(error)
        if errors:
            for error in errors:
                if isinstance(error, SessionStateUnavailable):
                    raise error
            raise errors[0]
        if code == 1:
            observe_admit("banned")
            raise AccountLocked("账号失败次数已达阈值，请使用正确凭据重试")
        if code == 2:
            observe_admit("banned")
            raise RateLimited("登录来源 IP 已临时封禁")
        if code == 3:
            observe_admit("prehash")
            raise RateLimited("登录请求过于频繁，请稍后重试")
        if code != 0:
            observe_admit("unavailable")
            raise SessionStateUnavailable("auth guard returned unknown state")
        observe_admit("allowed")

    @staticmethod
    def _parse_guard_result(raw: Any) -> tuple[Any, ...]:
        if not isinstance(raw, (list, tuple)) or len(raw) not in {7, 11}:
            raise SessionStateUnavailable("auth guard returned invalid state")
        code = int(raw[0])
        lock_transition = str(raw[1])
        user_count = int(raw[2])
        ban_transition = str(raw[3])
        ip_count = int(raw[4])
        lock_ttl = int(raw[5])
        ban_ttl = int(raw[6])
        if len(raw) == 7:
            return (
                code,
                lock_transition,
                user_count,
                ban_transition,
                ip_count,
                lock_ttl,
                ban_ttl,
                lock_transition,
                ban_transition,
                "writing" if lock_transition else "",
                "writing" if ban_transition else "",
            )
        return (
            code,
            lock_transition,
            user_count,
            ban_transition,
            ip_count,
            lock_ttl,
            ban_ttl,
            str(raw[7]),
            str(raw[8]),
            str(raw[9]),
            str(raw[10]),
        )

    async def _settle_transition(
        self,
        *,
        action: str,
        transition_id: str,
        lease_id: str,
        audit_state: str,
        provider_code: str,
        result_code: str,
        count: int,
        remaining_ttl_seconds: int,
        ip: str,
    ) -> BaseException | None:
        try:
            await self._commit_transition(
                action=action,
                transition_id=transition_id,
                lease_id=lease_id,
                audit_state=audit_state,
                provider_code=provider_code,
                result_code=result_code,
                count=count,
                remaining_ttl_seconds=remaining_ttl_seconds,
                ip=ip,
            )
        except BaseException as error:
            return error
        return None

    async def _commit_transition(
        self,
        *,
        action: str,
        transition_id: str,
        lease_id: str,
        audit_state: str,
        provider_code: str,
        result_code: str,
        count: int,
        remaining_ttl_seconds: int,
        ip: str,
    ) -> None:
        if self.security_events is None:
            return
        if not lease_id:
            if audit_state == "audited":
                return
            raise SessionStateUnavailable("auth security audit pending")
        if audit_state == "writing":
            observe_transition_claim(action, self.owner)  # type: ignore[arg-type]
        await self.store.eval(
            self._BIND_LUA,
            1,
            f"auth:audit:transition:{transition_id}",
            provider_code,
            result_code,
            str(max(1, count)),
            str(max(1, remaining_ttl_seconds)),
            ip,
            action,
        )
        started = monotonic()
        try:
            await self.security_events.ensure_transition(
                AuthSecurityTransition(
                    action=action,  # type: ignore[arg-type]
                    transition_id=transition_id,
                    provider_code=provider_code,
                    result_code=result_code,  # type: ignore[arg-type]
                    count=count,
                    remaining_ttl_seconds=remaining_ttl_seconds,
                    ip=ip,
                )
            )
        except Exception as error:
            observe_transition_database_duration(action, monotonic() - started)  # type: ignore[arg-type]
            outcome = await self._mark_transition_retry(transition_id, lease_id)
            observe_transition_retry(action, _error_class(error))  # type: ignore[arg-type]
            if outcome < 0:
                observe_transition_dead(action)  # type: ignore[arg-type]
                LOGGER.critical("auth transition audit entered dead state")
            raise
        observe_transition_database_duration(action, monotonic() - started)  # type: ignore[arg-type]
        await self.store.eval(
            self._ACK_LUA,
            1,
            f"auth:audit:transition:{transition_id}",
            lease_id,
            str(max(1, remaining_ttl_seconds)),
        )

    async def _mark_transition_retry(self, transition_id: str, lease_id: str) -> int:
        raw = await self.store.eval(
            self._FAIL_LUA,
            1,
            f"auth:audit:transition:{transition_id}",
            lease_id,
            str(AUDIT_RECOVERY_TTL_S),
            str(MAX_TRANSITION_ATTEMPTS),
            str(MAX_TRANSITION_RECOVERY_MS),
        )
        return int(raw or 0)

    async def scan_due_transitions(self, limit: int = 20) -> tuple[str, ...]:
        raw = await self.store.eval(self._SCAN_DUE_LUA, 1, AUDIT_DUE_KEY, str(limit))
        if raw is None:
            return ()
        if isinstance(raw, (bytes, str)):
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return (text,) if text else ()
        return tuple(str(item) for item in raw if item)

    async def claim_due_transition(self, transition_id: str) -> tuple[str, ...]:
        raw = await self.store.eval(
            self._RECONCILE_CLAIM_LUA,
            0,
            transition_id,
            str(uuid4()),
            str(self.lease_ms),
            str(AUDIT_RECOVERY_TTL_S),
        )
        if not isinstance(raw, (list, tuple)) or len(raw) < 8:
            raise SessionStateUnavailable("auth transition claim unavailable")
        return tuple("" if item is None else str(item) for item in raw[:9])

    async def persist_claimed_transition(
        self,
        *,
        action: str,
        transition_id: str,
        lease_id: str,
        audit_state: str,
        provider_code: str,
        result_code: str,
        count: int,
        remaining_ttl_seconds: int,
        ip: str,
    ) -> None:
        """Reconciler 与请求路径共用同一 ACK/FAIL 合同。"""

        await self._commit_transition(
            action=action,
            transition_id=transition_id,
            lease_id=lease_id,
            audit_state=audit_state,
            provider_code=provider_code,
            result_code=result_code,
            count=count,
            remaining_ttl_seconds=remaining_ttl_seconds,
            ip=ip,
        )

    async def due_stats(self) -> tuple[int, int]:
        raw = await self.store.eval(self._DUE_STATS_LUA, 1, AUDIT_DUE_KEY)
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            return 0, 0
        return int(raw[0] or 0), int(raw[1] or 0)

    async def record_bound_success(self, username: str) -> None:
        """仅在权威账号/身份归属确认后清除主体失败状态。"""

        await self.store.delete(self._user_key("fail", username))
        await self.store.delete(self._user_key("lock", username))

    async def record_provider_success(self, provider_code: str) -> None:
        """Provider 凭据校验成功只证明容量健康，不证明账号归属。"""

        await self.store.delete(self._provider_capacity_key(provider_code))


class AuthService:
    """所有 Provider 共享失败阈值与 IP 防护，且主体状态不按来源拆分。"""

    def __init__(self, providers: AuthProviderRegistry, guard: LoginGuard) -> None:
        self.providers = providers
        self.guard = guard

    async def authenticate(
        self,
        provider_code: str,
        login_name: str,
        password: str,
        ip: str,
        *,
        purpose: AuthenticationPurpose = "login",
    ) -> AuthenticatedIdentity:
        normalized = normalize_login_name(login_name)
        await self.guard.admit(provider_code, normalized, ip)
        if provider_code.casefold() != "local":
            await self.guard.check_provider_capacity(provider_code)
        try:
            identity = await self.providers.authenticate(
                provider_code,
                normalized,
                password,
                purpose=purpose,
            )
        except InvalidCredentials:
            policy = await self.guard.snapshot()
            await self.guard.record_failure(normalized, ip, provider_code, policy=policy)
            raise
        except ProviderCapacityUnavailable:
            if provider_code.casefold() != "local":
                await self.guard.record_capacity_failure(provider_code)
            raise
        await self.guard.record_provider_success(provider_code)
        return identity

    async def record_bound_success(self, username: str) -> None:
        """由应用门面在 resolve_identity 成功后确认登录主体。"""

        await self.guard.record_bound_success(normalize_login_name(username))
