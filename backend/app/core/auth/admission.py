"""Redis 原子登录准入：不可退工作预算、来源预留与实际线程生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.core.auth.admission_policy import ACTIVE_KEY, POLICY_KEY, WORK_KEY, AdmissionPolicy
from app.core.auth.backends import AuthenticationPurpose, SessionStateUnavailable
from app.core.auth.capacity_metrics import observe_source
from app.core.bounded_executor import BoundedWorkScope

_FINISHING_TASKS: set[asyncio.Task[None]] = set()


async def drain_login_admissions() -> None:
    """API 停机先排空实际线程及 Redis 释放，再关闭共享连接。"""

    while _FINISHING_TASKS:
        await asyncio.gather(*tuple(_FINISHING_TASKS), return_exceptions=True)


class AdmissionBusy(RuntimeError):
    """未读取密码/连接 Provider 的有界重试；不同于凭据失败导致的 IP ban。"""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("认证容量繁忙，请稍后重试")
        self.retry_after_s = max(1, min(retry_after_s, 300))


@dataclass(frozen=True, slots=True, repr=False)
class AdmissionReservation:
    """仅进程内传递，不能从请求解析或进入认证响应。"""

    request_id: str
    source: str
    profile: str
    revision: int
    digest: str
    token: str

    @property
    def keys(self) -> tuple[str, str, str]:
        prefix = f"auth:admission:source:{self.source}"
        return prefix + ":bucket", prefix + ":window", "auth:admission:request:" + self.request_id


ADMIT_LUA = """
-- auth-source-admit-v1
local function kind(key) return redis.call('TYPE', key).ok end
if kind(KEYS[1]) ~= 'hash' or kind(KEYS[2]) ~= 'hash' or kind(KEYS[3]) ~= 'hash'
  or redis.call('HGET', KEYS[3], '_schema') ~= '1' then return {-1,0} end
local policy = redis.call('HMGET', KEYS[1], 'revision', 'digest')
if policy[1] ~= ARGV[1] or policy[2] ~= ARGV[2] then return {-1,0} end
for i=4,5 do
  local t=kind(KEYS[i])
  if t ~= 'none' and t ~= 'hash' then return {-1,0} end
end
if kind(KEYS[6]) ~= 'none' then return {-1,0} end
local tm = redis.call('TIME')
local now = tonumber(tm[1])*1000 + math.floor(tonumber(tm[2])/1000)
local active = redis.call('HGETALL', KEYS[3])
local count, source_count, provider_count = 0,0,0
for i=1,#active,2 do
  if active[i] ~= '_schema' then
    local source, provider, deadline = string.match(active[i+1], '^([a-f0-9]+):([a-z]+):(%d+)$')
    if not deadline or tonumber(deadline) <= now then return {-1,0} end
    count = count+1
    if source == ARGV[4] then source_count = source_count+1 end
    if provider == ARGV[5] then provider_count = provider_count+1 end
  end
end
if count >= tonumber(ARGV[11]) or source_count >= tonumber(ARGV[12])
   or provider_count >= tonumber(ARGV[13]) then return {1,1} end

local function bucket(key, capacity, refill, fresh)
  if kind(key) == 'none' and fresh then return capacity,now,ARGV[3] end
  local state = redis.call('HMGET', key, 'tokens','updated_ms','generation')
  local tokens, updated = tonumber(state[1]),tonumber(state[2])
  if not tokens or tokens < 0 or not updated or updated < 0 then return nil end
  if fresh and not state[3] then return nil end
  tokens=math.min(capacity, tokens + math.max(0,now-updated)/refill)
  return tokens,math.max(now,updated),state[3]
end
local work, work_at = bucket(KEYS[2], tonumber(ARGV[9]), tonumber(ARGV[10]), false)
local tokens, updated, generation = bucket(KEYS[4], tonumber(ARGV[6]), tonumber(ARGV[8]), true)
if not work or not tokens then return {-1,0} end
local window, window_generation = 0, ARGV[3]
if kind(KEYS[5]) == 'hash' then
  local state = redis.call('HMGET', KEYS[5], 'count','generation')
  window,window_generation = tonumber(state[1]),state[2]
  if not window or window < 0 or not window_generation then return {-1,0} end
end
if window >= tonumber(ARGV[7]) then return {1,math.max(1,redis.call('TTL',KEYS[5]))} end
if tokens < 1 then return {1,math.max(1,math.ceil((1-tokens)*tonumber(ARGV[8])/1000))} end
if work < 1 then return {1,math.max(1,math.ceil((1-work)*tonumber(ARGV[10])/1000))} end

redis.call('HSET', KEYS[2], 'tokens',work-1, 'updated_ms',work_at)
redis.call('HSET', KEYS[4], 'tokens',tokens-1,'updated_ms',updated,'generation',generation)
-- 桶保留至少一次完整恢复所需时长，防止较慢 refill 因 TTL 缩短而补满。
redis.call('PEXPIRE', KEYS[4], math.max(300000,tonumber(ARGV[6])*tonumber(ARGV[8])))
redis.call('HSET', KEYS[5], 'count',window+1,'generation',window_generation)
if window == 0 and redis.call('PTTL',KEYS[5]) < 0 then redis.call('EXPIRE',KEYS[5],300) end
redis.call('HSET', KEYS[3], ARGV[3], ARGV[4]..':'..ARGV[5]..':'..tostring(now+120000))
redis.call('HSET', KEYS[6], 'token',ARGV[14], 'bucket_generation',generation,
 'window_generation',window_generation, 'revision',ARGV[1], 'digest',ARGV[2],
 'capacity',ARGV[6], 'work_done','0')
redis.call('EXPIRE', KEYS[6],600)
return {0,0}
"""

RELEASE_LUA = """
-- auth-source-work-release-v1
if redis.call('HGET',KEYS[1],'_schema') ~= '1'
   or redis.call('HGET',KEYS[2],'token') ~= ARGV[2] then return -1 end
if redis.call('HGET',KEYS[2],'work_done') == '1' then return 1 end
if not redis.call('HGET',KEYS[1],ARGV[1]) then return -1 end
redis.call('HDEL',KEYS[1],ARGV[1])
redis.call('HSET',KEYS[2],'work_done','1')
return 1
"""

SETTLE_LUA = """
-- auth-source-success-v1
if redis.call('EXISTS',KEYS[4]) == 0 then return 0 end
if redis.call('HGET',KEYS[4],'token') ~= ARGV[1]
   or redis.call('HGET',KEYS[4],'work_done') ~= '1' then return -1 end
local policy = redis.call('HMGET',KEYS[1],'revision','digest')
if not policy[1] or not policy[2] then return -1 end
if redis.call('HGET',KEYS[4],'revision') ~= ARGV[2]
   or redis.call('HGET',KEYS[4],'digest') ~= ARGV[3] then return -1 end
-- 旧世代/过期来源只丢弃预留，不能对新策略或新窗口退款。
if policy[1] ~= ARGV[2] or policy[2] ~= ARGV[3] or ARGV[4] ~= 'shared' then
  redis.call('DEL',KEYS[4]); return 0
end
local expected = redis.call('HMGET',KEYS[4], 'bucket_generation','window_generation','capacity')
local capacity = tonumber(expected[3])
if not expected[1] or not expected[2] or not capacity then return -1 end
local bucket = redis.call('HMGET',KEYS[2],'generation','tokens')
local window = redis.call('HMGET',KEYS[3],'generation','count')
if redis.call('EXISTS',KEYS[2]) == 1 and not bucket[1] then return -1 end
if redis.call('EXISTS',KEYS[3]) == 1 and not window[1] then return -1 end
if bucket[1] and (not tonumber(bucket[2]) or tonumber(bucket[2]) < 0) then return -1 end
if window[1] and (not tonumber(window[2]) or tonumber(window[2]) < 0) then return -1 end
if bucket[1] == expected[1] then
  redis.call('HSET',KEYS[2],'tokens',math.min(capacity,tonumber(bucket[2])+1))
end
if window[1] == expected[2] and tonumber(window[2]) > 0 then
  redis.call('HSET',KEYS[3],'count',tonumber(window[2])-1)
end
redis.call('DEL',KEYS[4])
return 1
"""


class LoginAdmission:
    def __init__(
        self,
        store: Any,
        policy_loader: Callable[[], Awaitable[AdmissionPolicy]],
        *,
        key: bytes,
    ) -> None:
        if len(key) < 32:
            raise ValueError("admission source key is too short")
        self.store = store
        self.policy_loader = policy_loader
        self.key = key
        self._finishing: set[asyncio.Task[None]] = set()

    async def admit(
        self, provider: str, ip: str, *, purpose: AuthenticationPurpose = "login",
    ) -> AdmissionReservation:
        """只使用可信 IP 与只读快照；所有额度/并发检查先于 Provider 读取。"""

        policy = await self.policy_loader()
        try:
            address = ipaddress.ip_address(ip)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            canonical_ip = str(address)
        except ValueError:
            canonical_ip = "0.0.0.0"
        source = hmac.new(
            self.key, b"auth-source-v1:" + canonical_ip.encode(), hashlib.sha256
        ).hexdigest()
        profile = policy.approvals.select(canonical_ip)
        reservation = AdmissionReservation(
            str(uuid4()),
            source,
            profile,
            policy.revision,
            policy.digest,
            str(uuid4()),
        )
        limits = policy.limits
        burst, window, refill = (5, 20, 15000)
        if profile == "shared":
            burst, window, refill = (
                limits.shared_burst,
                limits.shared_window,
                limits.shared_refill_ms,
            )
        provider_class = "ad"
        if provider.casefold() == "local":
            provider_class = "local" if purpose == "login" else "reauth"
        attempt = asyncio.create_task(
            self.store.eval(
                ADMIT_LUA,
                6,
                POLICY_KEY,
                WORK_KEY,
                ACTIVE_KEY,
                *reservation.keys,
                str(policy.revision),
                policy.digest,
                reservation.request_id,
                source,
                provider_class,
                str(burst),
                str(window),
                str(refill),
                str(limits.global_burst),
                str(limits.global_refill_ms),
                str(limits.global_concurrent),
                str(limits.source_concurrent),
                "8" if provider_class == "ad" else "2",
                reservation.token,
            )
        )
        try:
            result = await asyncio.shield(attempt)
        except asyncio.CancelledError:
            # EVAL 可能已获准；拥有者继续读取最终结果，未启动 Provider 时安全释放。
            async def cancel_admission() -> None:
                outcome = await attempt
                if isinstance(outcome, (list, tuple)) and outcome[0] == 0:
                    await self.release_work(reservation, BoundedWorkScope())

            self._own(asyncio.create_task(cancel_admission(), name="auth-admission-cancel"))
            raise
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise SessionStateUnavailable("auth admission state unavailable")
        if result[0] == 1:
            observe_source(profile, "limited")
            raise AdmissionBusy(int(result[1]))
        if result[0] != 0:
            observe_source(profile, "unavailable")
            raise SessionStateUnavailable("auth admission state unavailable")
        observe_source(profile, "allowed")
        return reservation

    async def release_work(self, reservation: AdmissionReservation, work: BoundedWorkScope) -> None:
        """线程真正完成才释放全局槽；取消原请求不会取消此清理任务。"""

        async def finish() -> None:
            await work.wait_finished()
            result = await self.store.eval(
                RELEASE_LUA,
                2,
                ACTIVE_KEY,
                reservation.keys[2],
                reservation.request_id,
                reservation.token,
            )
            if result != 1:
                raise SessionStateUnavailable("auth admission work state unavailable")

        task = asyncio.create_task(finish(), name="auth-admission-finish")
        self._own(task)
        if not work.has_pending:
            await asyncio.shield(task)

    def _own(self, task: asyncio.Task[None]) -> None:
        self._finishing.add(task)
        _FINISHING_TASKS.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._finishing.discard(completed)
            _FINISHING_TASKS.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def complete(self, reservation: AdmissionReservation, ip: str) -> None:
        """完整成功仅结算原来源预留；工作预算和独立 IP ban 不退款。"""

        policy = await self.policy_loader()
        profile = policy.approvals.select(ip) if reservation.profile == "shared" else "internet"
        result = await self.store.eval(
            SETTLE_LUA,
            4,
            POLICY_KEY,
            *reservation.keys,
            reservation.token,
            str(reservation.revision),
            reservation.digest,
            profile,
        )
        if result not in {0, 1}:
            observe_source(profile, "unavailable", refund=True)
            raise SessionStateUnavailable("auth admission settlement unavailable")
        observe_source(profile, "refunded" if result == 1 else "skipped", refund=True)
