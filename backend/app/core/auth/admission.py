"""Redis 原子登录准入：不可退工作预算、来源预留与实际线程生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
from weakref import WeakSet

from app.core.auth.admission_policy import ACTIVE_KEY, POLICY_KEY, WORK_KEY, AdmissionPolicy
from app.core.auth.backends import AuthenticationPurpose, SessionStateUnavailable
from app.core.auth.capacity_metrics import observe_recovery, observe_source
from app.core.bounded_executor import BoundedWorkScope

_FINISHING_TASKS: set[asyncio.Task[None]] = set()
_GUARDS: WeakSet[LoginAdmission] = WeakSet()
_DRAIN_TASK: asyncio.Task[bool] | None = None
RECOVERY_ATTEMPTS = 5
REDIS_TIMEOUT_S = 1.0
DRAIN_TIMEOUT_S = 10.0
MAX_OWNERS = 128
TIME_LUA = "return redis.call('TIME')"
_LOG = logging.getLogger(__name__)


async def drain_login_admissions() -> bool:
    """有界停机：封闭新准入，停止所有 Redis 使用任务，显式报告未恢复事实。"""
    global _DRAIN_TASK
    if _DRAIN_TASK is None or _DRAIN_TASK.done():
        _DRAIN_TASK = asyncio.create_task(_drain(), name="auth-admission-drain")
    return await asyncio.shield(_DRAIN_TASK)


async def _drain() -> bool:
    guards = tuple(guard for guard in _GUARDS if guard._loop is asyncio.get_running_loop())
    for guard in guards:
        guard._stopping = True
        for owner in tuple(guard._owners.values()):
            if not guard._closed and owner.phase in {"cancel", "release"} and (
                owner.task is None or owner.task.done()
            ):
                guard._start_recovery(owner)
    try:
        async with asyncio.timeout(DRAIN_TIMEOUT_S):
            while _FINISHING_TASKS:
                await asyncio.gather(*tuple(_FINISHING_TASKS), return_exceptions=True)
    except TimeoutError:
        observe_recovery("shutdown_incomplete")
    # 待确认 ADMIT 与清理任务都必须在 Redis 连接关闭前停止。
    for guard in guards:
        guard._closed = True
    tasks = {task for guard in guards for task in guard._attempts} | _FINISHING_TASKS
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    complete = not any(guard._owners for guard in guards)
    if not complete:
        observe_recovery("shutdown_incomplete")
        _LOG.warning("auth_admission_shutdown_incomplete")
    return complete


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
    binding: str = ""

    @property
    def keys(self) -> tuple[str, str, str]:
        prefix = f"auth:admission:source:{self.source}"
        return prefix + ":bucket", prefix + ":window", "auth:admission:request:" + self.request_id


@dataclass(slots=True)
class _AdmissionOwner:
    reservation: AdmissionReservation
    phase: str = "admit"
    work: BoundedWorkScope | None = None
    task: asyncio.Task[None] | None = None


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
local tm = redis.call('TIME')
local now = tonumber(tm[1])*1000 + math.floor(tonumber(tm[2])/1000)
local issued = tonumber(ARGV[15])
if not issued or issued > now or now-issued > 30000 then return {-1,0} end
local request_kind = kind(KEYS[6])
if request_kind == 'hash' then
  local saved = redis.call('HMGET',KEYS[6],'token','binding','state','active_value')
  if saved[1] ~= ARGV[14] or saved[2] ~= ARGV[16] then return {-1,0} end
  if saved[3] ~= 'active' then return {2,0} end
  if saved[4] and redis.call('HGET',KEYS[3],ARGV[3]) == saved[4] then return {0,0} end
  return {-1,0}
end
if request_kind ~= 'none' or redis.call('HGET',KEYS[3],ARGV[3]) ~= false then
  return {-1,0}
end
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
local active_value = ARGV[4]..':'..ARGV[5]..':'..tostring(issued+120000)
redis.call('HSET', KEYS[3], ARGV[3], active_value)
redis.call('HSET', KEYS[6], 'token',ARGV[14], 'bucket_generation',generation,
 'window_generation',window_generation, 'revision',ARGV[1], 'digest',ARGV[2],
 'capacity',ARGV[6], 'work_done','0','state','active',
 'binding',ARGV[16], 'active_value',active_value)
-- 非终态证明不得在实际工作完成前过期。
redis.call('PERSIST', KEYS[6])
return {0,0}
"""

CANCEL_LUA = """
-- auth-source-cancel-v2: 只终结本进程证明从未进入 Provider 的原命令。
local active_kind = redis.call('TYPE',KEYS[1]).ok
local request_kind = redis.call('TYPE',KEYS[2]).ok
if active_kind ~= 'hash' or redis.call('HGET',KEYS[1],'_schema') ~= '1' then return -1 end
if request_kind == 'none' then
  if redis.call('HGET',KEYS[1],ARGV[1]) ~= false then return -1 end
  redis.call('HSET',KEYS[2],'token',ARGV[2],'binding',ARGV[3],
    'state','cancelled','work_done','1')
elseif request_kind == 'hash' then
  local saved = redis.call('HMGET',KEYS[2],'token','binding','state','active_value')
  if saved[1] ~= ARGV[2] or saved[2] ~= ARGV[3] then return -1 end
  if saved[3] == 'cancelled' then return 1 end
  if saved[3] ~= 'active' or not saved[4]
     or redis.call('HGET',KEYS[1],ARGV[1]) ~= saved[4] then return -1 end
  redis.call('HDEL',KEYS[1],ARGV[1])
  redis.call('HSET',KEYS[2],'state','cancelled','work_done','1')
else return -1 end
-- 终结标记的寿命远长于原 ADMIT 的服务器时间接受窗口。
redis.call('EXPIRE',KEYS[2],600)
return 1
"""

RELEASE_LUA = """
-- auth-source-work-release-v2
if redis.call('TYPE',KEYS[1]).ok ~= 'hash'
   or redis.call('TYPE',KEYS[2]).ok ~= 'hash'
   or redis.call('HGET',KEYS[1],'_schema') ~= '1' then return -1 end
local saved = redis.call('HMGET',KEYS[2],'token','binding','state','active_value')
if saved[1] ~= ARGV[2] or saved[2] ~= ARGV[3] then return -1 end
if saved[3] == 'released' or saved[3] == 'settled' then return 1 end
if saved[3] ~= 'active' or not saved[4]
   or redis.call('HGET',KEYS[1],ARGV[1]) ~= saved[4] then return -1 end
redis.call('HDEL',KEYS[1],ARGV[1])
redis.call('HSET',KEYS[2],'work_done','1','state','released')
redis.call('EXPIRE',KEYS[2],600)
return 1
"""

SETTLE_LUA = """
-- auth-source-success-v1
if redis.call('EXISTS',KEYS[4]) == 0 then return 0 end
if redis.call('HGET',KEYS[4],'token') ~= ARGV[1]
   or redis.call('HGET',KEYS[4],'work_done') ~= '1' then return -1 end
local state = redis.call('HGET',KEYS[4],'state')
if state == 'settled' then return 0 end
if state ~= 'released' then return -1 end
local policy = redis.call('HMGET',KEYS[1],'revision','digest')
if not policy[1] or not policy[2] then return -1 end
if redis.call('HGET',KEYS[4],'revision') ~= ARGV[2]
   or redis.call('HGET',KEYS[4],'digest') ~= ARGV[3] then return -1 end
-- 旧世代/过期来源只丢弃预留，不能对新策略或新窗口退款。
if policy[1] ~= ARGV[2] or policy[2] ~= ARGV[3] or ARGV[4] ~= 'shared' then
  redis.call('HSET',KEYS[4],'state','settled'); return 0
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
redis.call('HSET',KEYS[4],'state','settled')
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
        self._attempts: set[asyncio.Task[Any]] = set()
        self._owners: dict[str, _AdmissionOwner] = {}
        self._stopping = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        _GUARDS.add(self)

    async def admit(
        self, provider: str, ip: str, *, purpose: AuthenticationPurpose = "login",
    ) -> AdmissionReservation:
        """只使用可信 IP 与只读快照；所有额度/并发检查先于 Provider 读取。"""

        self._loop = asyncio.get_running_loop()
        if self._stopping or len(self._owners) >= MAX_OWNERS:
            observe_recovery("capacity_blocked")
            raise SessionStateUnavailable("auth admission recovery capacity unavailable")
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
        # TIME 没有写副作用；只使用本次原始服务器时间，重试不得延长接受窗口。
        try:
            async with asyncio.timeout(REDIS_TIMEOUT_S):
                second, micro = await self.store.eval(TIME_LUA, 0)
            issued_ms = int(second) * 1000 + int(micro) // 1000
        except Exception as exc:
            raise SessionStateUnavailable("auth admission clock unavailable") from exc
        arguments = (
            str(policy.revision), policy.digest, reservation.request_id, source,
            provider_class, str(burst), str(window), str(refill),
            str(limits.global_burst), str(limits.global_refill_ms),
            str(limits.global_concurrent), str(limits.source_concurrent),
            "8" if provider_class == "ad" else "2", reservation.token, str(issued_ms),
        )
        binding = hmac.new(self.key, "\x1f".join(arguments).encode(), hashlib.sha256).hexdigest()
        reservation = AdmissionReservation(
            reservation.request_id, source, profile, policy.revision, policy.digest,
            reservation.token, binding,
        )
        # 中间 await 期间可能已进入停机或其它请求已用完所有者额度。
        if self._stopping or len(self._owners) >= MAX_OWNERS:
            raise SessionStateUnavailable("auth admission recovery capacity unavailable")
        owner = _AdmissionOwner(reservation)
        self._owners[reservation.request_id] = owner
        attempt = asyncio.create_task(self.store.eval(
            ADMIT_LUA, 6, POLICY_KEY, WORK_KEY, ACTIVE_KEY, *reservation.keys,
            *arguments, binding,
        ))
        self._attempts.add(attempt)
        attempt.add_done_callback(self._attempts.discard)
        try:
            async with asyncio.timeout(REDIS_TIMEOUT_S):
                result = await asyncio.shield(attempt)
            if not isinstance(result, (list, tuple)) or len(result) != 2:
                raise SessionStateUnavailable("auth admission state unavailable")
            if result[0] == 1:
                # 已收到确定拒绝，也保留终结栅栏，避免原命令迟到重放。
                owner.phase = "cancel"
                self._start_recovery(owner)
                observe_source(profile, "limited")
                raise AdmissionBusy(int(result[1]))
            if result[0] != 0 or self._stopping:
                raise SessionStateUnavailable("auth admission state unavailable")
        except AdmissionBusy:
            raise
        except BaseException as exc:
            attempt.cancel()
            # 取消 Python 等待不证明 Redis 未执行，必须独立持有精确取消责任。
            owner.phase = "cancel"
            if not self._closed:
                self._start_recovery(owner)
            if isinstance(exc, Exception):
                raise SessionStateUnavailable("auth admission state unavailable") from exc
            raise
        owner.phase = "admitted"
        observe_source(profile, "allowed")
        return reservation

    async def release_work(self, reservation: AdmissionReservation, work: BoundedWorkScope) -> None:
        """线程真正完成才释放；所有者保留到确认，恢复耗尽则继续明确阻塞。"""
        owner = self._owners.get(reservation.request_id)
        if owner is None or owner.reservation != reservation:
            raise SessionStateUnavailable("auth admission owner unavailable")
        if owner.phase == "admitted":
            owner.phase, owner.work = "release", work
        elif owner.phase != "release" or owner.work is not work:
            raise SessionStateUnavailable("auth admission owner state unavailable")
        task = self._start_recovery(owner)
        if not work.has_pending:
            await asyncio.shield(task)

    def _start_recovery(self, owner: _AdmissionOwner) -> asyncio.Task[None]:
        if self._closed:
            raise SessionStateUnavailable("auth admission recovery stopped")
        if owner.task is not None and not owner.task.done():
            return owner.task
        task = asyncio.create_task(self._recover(owner), name="auth-admission-recovery")
        owner.task = task
        self._finishing.add(task)
        _FINISHING_TASKS.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._finishing.discard(completed)
            _FINISHING_TASKS.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    async def _recover(self, owner: _AdmissionOwner) -> None:
        if owner.work is not None:
            await owner.work.wait_finished()
        reservation = owner.reservation
        for attempt in range(RECOVERY_ATTEMPTS):
            try:
                async with asyncio.timeout(REDIS_TIMEOUT_S):
                    result = await self.store.eval(
                        CANCEL_LUA if owner.phase == "cancel" else RELEASE_LUA,
                        2, ACTIVE_KEY, reservation.keys[2], reservation.request_id,
                        reservation.token, reservation.binding,
                    )
                if result != 1:
                    # 错误 token/损坏事实不能通过重复尝试或清空扩大权限。
                    break
                self._owners.pop(reservation.request_id, None)
                observe_recovery("confirmed")
                return
            except Exception:
                observe_recovery("retry")
                if attempt + 1 < RECOVERY_ATTEMPTS:
                    await asyncio.sleep(min(0.05 * 2**attempt, 0.4))
        observe_recovery("blocked")
        raise SessionStateUnavailable("auth admission recovery incomplete")

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
