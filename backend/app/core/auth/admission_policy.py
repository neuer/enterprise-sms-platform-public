"""登录来源的部署信任边界与 sys_config 阈值；匿名路径只读已验证快照。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from app.core.auth.backends import SessionStateUnavailable
from app.core.runtime_resources import database_engine
from app.settings import Settings

POLICY_KEY = "auth:admission:policy"
WORK_KEY = "auth:admission:work"
ACTIVE_KEY = "auth:admission:active"
CONFIG_KEY = "auth_admission_policy"
DEFAULT_CONFIG = (
    '{"version":1,"shared_burst":100,"shared_window":200,"shared_refill_ms":1000,'
    '"global_burst":8,"global_refill_ms":250,"global_concurrent":4,"source_concurrent":2}'
)


class AdmissionLimits(BaseModel):
    """业务阈值有硬安全上界；来源清单不接受 sys_config 或客户端输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    shared_burst: int = Field(default=100, ge=5, le=200)
    shared_window: int = Field(default=200, ge=20, le=500)
    shared_refill_ms: int = Field(default=1000, ge=250, le=15000)
    global_burst: int = Field(default=8, ge=1, le=16)
    global_refill_ms: int = Field(default=250, ge=100, le=15000)
    global_concurrent: int = Field(default=4, ge=1, le=16)
    source_concurrent: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def validate_bounds(self) -> AdmissionLimits:
        if self.shared_window < self.shared_burst:
            raise ValueError("shared window must cover burst")
        if self.source_concurrent > self.global_concurrent:
            raise ValueError("source concurrency exceeds global capacity")
        return self


class SourceApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cidr: str = Field(max_length=64, repr=False)
    expires_at: datetime
    approval_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    @model_validator(mode="after")
    def validate_network(self) -> SourceApproval:
        network = ipaddress.ip_network(self.cidr, strict=True)
        # 只允许小范围精确出口；不把任意 NAT64/IPv6 前缀折算成 IPv4。
        if network.prefixlen < (28 if network.version == 4 else 120):
            raise ValueError("source approval network is too broad")
        address = network.network_address
        if address.is_unspecified or address.is_multicast:
            raise ValueError("invalid source approval network")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            raise ValueError("use canonical IPv4 source approval")
        if self.expires_at.tzinfo is None:
            raise ValueError("source approval expiry requires timezone")
        return self


class SourceApprovals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    profiles: tuple[SourceApproval, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def reject_overlap(self) -> SourceApprovals:
        networks = [ipaddress.ip_network(item.cidr) for item in self.profiles]
        for index, first in enumerate(networks):
            if any(first.overlaps(second) for second in networks[index + 1 :]):
                raise ValueError("source approval networks overlap")
        return self

    @classmethod
    def from_file(cls, path: Path | None) -> SourceApprovals:
        if path is None:
            return cls()
        try:
            with path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("oversized source profile")
            return cls.model_validate_json(raw)
        except Exception:
            raise SessionStateUnavailable("auth source profile unavailable") from None

    def select(self, ip: str, now: datetime | None = None) -> Literal["internet", "shared"]:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return "internet"
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        instant = now or datetime.now(UTC)
        for item in self.profiles:
            if instant < item.expires_at and address in ipaddress.ip_network(item.cidr):
                return "shared"
        return "internet"


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    revision: int
    limits: AdmissionLimits
    approvals: SourceApprovals

    @property
    def digest(self) -> str:
        value = [self.limits.model_dump(mode="json"), self.approvals.model_dump(mode="json")]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


PUBLISH_LUA = """
-- auth-admission-policy-v1
local current = redis.call('HMGET', KEYS[1], 'revision', 'digest')
local revision = tonumber(ARGV[1])
if not revision or revision < 1 then return -1 end
if current[1] then
  local previous = tonumber(current[1])
  if not previous or not current[2] or previous > revision then return -1 end
  if previous == revision and current[2] ~= ARGV[2] then return -1 end
  -- 已发布策略丢失工作事实时不重建满桶或空并发。
  if redis.call('TYPE', KEYS[2]).ok ~= 'hash'
     or redis.call('HGET', KEYS[3], '_schema') ~= '1' then return -1 end
  if previous < revision then
    local work = redis.call('HMGET',KEYS[2],'tokens','updated_ms','capacity','refill_ms')
    for i=1,4 do if not tonumber(work[i]) then return -1 end end
    local now = redis.call('TIME')
    local ms = tonumber(now[1])*1000 + math.floor(tonumber(now[2])/1000)
    -- 先按旧速率结算旧时间段，再切换参数；新速率不能追溯补发旧额度。
    local tokens = math.min(tonumber(work[3]),tonumber(work[1]) +
      math.max(0,ms-tonumber(work[2]))/tonumber(work[4]))
    redis.call('HSET',KEYS[2],'tokens',math.min(tonumber(ARGV[3]),tokens),
      'updated_ms',math.max(ms,tonumber(work[2])),
      'capacity',ARGV[3],'refill_ms',ARGV[4])
  end
else
  if ARGV[5] ~= 'bootstrap' then return -1 end
  if redis.call('EXISTS', KEYS[1], KEYS[2], KEYS[3]) ~= 0 then return -1 end
  local now = redis.call('TIME')
  local ms = tonumber(now[1])*1000 + math.floor(tonumber(now[2])/1000)
  -- 冷启动从零工作额度恢复，禁止重启获得额外突发额度。
  redis.call('HSET', KEYS[2], 'tokens', 0, 'updated_ms', ms,
    'capacity',ARGV[3],'refill_ms',ARGV[4])
  redis.call('HSET', KEYS[3], '_schema', '1')
end
redis.call('HSET', KEYS[1], 'revision', ARGV[1], 'digest', ARGV[2])
return 1
"""


class AdmissionPolicyRuntime:
    """后台单飞读取 PG、CAS 对齐 Redis；请求读取不触发回源或续期。"""

    def __init__(
        self,
        settings: Settings,
        store: Any,
        *,
        loader: Callable[[], Awaitable[AdmissionPolicy]] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.settings = settings
        self.store = store
        self.loader = loader or self._postgres
        self.clock = clock
        self.policy: AdmissionPolicy | None = None
        self.verified_at = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._published = False

    async def _postgres(self) -> AdmissionPolicy:
        approvals = SourceApprovals.from_file(self.settings.auth_source_profile_file)
        engine = database_engine(self.settings.database_url_for("auth"), component="background")
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT value, (EXTRACT(EPOCH FROM updated_at)*1000000)::bigint AS revision "
                    "FROM sys_config WHERE key = :key"
                ),
                {"key": CONFIG_KEY},
            )
            row = result.mappings().first()
        if row is None:
            raise SessionStateUnavailable("auth admission policy missing")
        return AdmissionPolicy(
            int(row["revision"]),
            AdmissionLimits.model_validate_json(row["value"]),
            approvals,
        )

    async def load(self) -> AdmissionPolicy:
        if self._stopped or self.policy is None or self.clock() - self.verified_at > 15:
            raise SessionStateUnavailable("auth admission policy unavailable")
        return self.policy

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self._stopped:
                raise SessionStateUnavailable("auth admission policy stopped")
            try:
                async with asyncio.timeout(2):
                    policy = await self.loader()
                    if self.policy is not None and policy.revision < self.policy.revision:
                        raise SessionStateUnavailable("auth admission policy stale")
                    result = await self.store.eval(
                        PUBLISH_LUA,
                        3,
                        POLICY_KEY,
                        WORK_KEY,
                        ACTIVE_KEY,
                        str(policy.revision),
                        policy.digest,
                        str(policy.limits.global_burst),
                        str(policy.limits.global_refill_ms),
                        "existing" if self._published else "bootstrap",
                    )
                    if result != 1:
                        raise SessionStateUnavailable("auth admission policy conflict")
                if self._stopped:
                    raise SessionStateUnavailable("auth admission policy stopped")
                self.policy = policy
                self._published = True
                self.verified_at = self.clock()
            except Exception:
                self.policy = None
                raise SessionStateUnavailable("auth admission policy unavailable") from None

    def start(self) -> None:
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="auth-admission-policy")

    async def _run(self) -> None:
        while True:
            with suppress(SessionStateUnavailable):
                await self.ensure_ready()
            await asyncio.sleep(5)

    async def stop(self) -> None:
        self._stopped = True
        self.policy = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


_RUNTIME: AdmissionPolicyRuntime | None = None


def get_admission_policy_runtime(settings: Settings) -> AdmissionPolicyRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        from app.core.auth.service import RedisKeyValue
        from app.core.runtime_resources import redis_client

        _RUNTIME = AdmissionPolicyRuntime(
            settings, RedisKeyValue(redis_client(settings.redis_auth_url))
        )
    return _RUNTIME
