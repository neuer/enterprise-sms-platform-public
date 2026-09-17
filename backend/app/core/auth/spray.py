"""跨实例失败行为信号；固定分桶、限时匿名事实，不采集密码。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any

from app.core.auth.admission_policy import AdmissionPolicy
from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.capacity_metrics import observe_spray
from app.core.auth.identity import normalize_login_name

# 两个方向各固定 4096 桶，每桶至多 32 个匿名位；碰撞只能增加失败响应延迟。
BUCKETS = 4096
WINDOW_SECONDS = 900
_RECORD = """
local counts = {}
for i = 1, 2 do
  local kind = redis.call('TYPE', KEYS[i]).ok
  if kind ~= 'none' and kind ~= 'hash' then return {-1, 0, -1, 0} end
  counts[i] = 0
  if kind == 'hash' then
    local entries = redis.call('HGETALL', KEYS[i])
    if redis.call('TTL', KEYS[i]) < 0 or #entries > 66 then return {-1, 0, -1, 0} end
    for j = 1, #entries, 2 do
      if entries[j] == 'count' then
        counts[i] = tonumber(entries[j + 1])
      elseif not string.match(entries[j], '^s%d+$') or
             tonumber(string.sub(entries[j], 2)) > 31 or
             entries[j] ~= 's' .. tostring(tonumber(string.sub(entries[j], 2))) or
             entries[j + 1] ~= '1' then
        return {-1, 0, -1, 0}
      end
    end
    if not counts[i] or counts[i] < 1 or counts[i] > 1000000 then
      return {-1, 0, -1, 0}
    end
  end
end
local result = {}
for i = 1, 2 do
  local count = math.min(counts[i] + 1, 1000000)
  redis.call('HSET', KEYS[i], 'count', count, ARGV[i], '1')
  if counts[i] == 0 then redis.call('EXPIRE', KEYS[i], ARGV[3]) end
  table.insert(result, count)
  table.insert(result, #redis.call('HGETALL', KEYS[i]) / 2 - 1)
end
return result
"""


class PasswordSprayGuard:
    """只处理已证实的凭据失败，不按用户名拒绝正确凭据。"""

    def __init__(
        self,
        store: Any,
        loader: Callable[[], Awaitable[AdmissionPolicy]],
        *,
        key: bytes,
    ) -> None:
        self.store = store
        self.loader = loader
        self.key = key

    def _digest(self, domain: bytes, value: str) -> bytes:
        return hmac.digest(self.key, domain + value.encode("utf-8"), hashlib.sha256)

    async def record_failure(self, username: str, ip: str) -> float:
        """返回失败响应延迟；不得传入密码，Redis 不确定时失败关闭。"""
        try:
            limits = (await self.loader()).limits
            address = ip_address(ip)
            address = getattr(address, "ipv4_mapped", None) or address
            account = self._digest(b"auth-spray-account-v1:", normalize_login_name(username))
            source = self._digest(b"auth-spray-source-v1:", str(address))
            raw = await self.store.eval(
                _RECORD,
                2,
                f"auth:spray:v1:account:{int.from_bytes(account[:2]) % BUCKETS}",
                f"auth:spray:v1:source:{int.from_bytes(source[:2]) % BUCKETS}",
                f"s{source[0] % 32}",
                f"s{account[0] % 32}",
                str(WINDOW_SECONDS),
            )
            account_count, sources, source_count, accounts = map(int, raw)
            if any(
                not 1 <= count <= 1000000 or not 1 <= distinct <= 32
                for count, distinct in ((account_count, sources), (source_count, accounts))
            ):
                raise ValueError("invalid spray state")
            count = max(
                (
                    count
                    for count, distinct in ((account_count, sources), (source_count, accounts))
                    if distinct >= limits.spray_sources
                ),
                default=0,
            )
            if count >= limits.spray_failures:
                level = "high" if count >= limits.spray_failures * 2 else "elevated"
                observe_spray(level)
                return min(1.0, limits.spray_delay_ms / 1000 * (2 if level == "high" else 1))
            return 0
        except Exception:
            observe_spray("unavailable")
            raise SessionStateUnavailable("auth spray state unavailable") from None

    async def delay(self, seconds: float) -> None:
        """让出事件循环；调用者必须先释放已完成的 Provider 工作预留。"""
        if seconds:
            await asyncio.sleep(seconds)
