"""撤销与 refresh 轮换的 Redis Lua 脚本及其内存等价实现。"""

from __future__ import annotations

from typing import Any

from app.core.auth.jwt_tokens import REFRESH_GRACE_SECONDS

REVOKE_SESSION_LUA_TAG = "auth-revoke-session-v1"
ROTATE_REFRESH_LUA_TAG = "auth-rotate-refresh-v2"

_REDIS_REVOCATION_LIB_LUA = r"""
local function redis_type(key)
  local current = redis.call('TYPE', key)
  if type(current) == 'table' then
    return current['ok']
  end
  return current
end

local function is_grace(value)
  if type(value) ~= 'string' then
    return false
  end
  local first = string.find(value, '\n', 1, true)
  if not first or first <= 1 then
    return false
  end
  if string.sub(value, 1, first - 1) ~= 'grace' then
    return false
  end
  local second = string.find(value, '\n', first + 1, true)
  if not second or second <= first + 1 or second >= #value then
    return false
  end
  return true
end

local function apply_jti_revocation(key, required_ms)
  local kind = redis_type(key)
  if kind == 'none' then
    redis.call('SET', key, '1', 'PX', required_ms)
    return 1
  end
  if kind ~= 'string' then
    return 0
  end
  local value = redis.call('GET', key)
  if value ~= '1' then
    return 0
  end
  local pttl = redis.call('PTTL', key)
  if pttl == -1 then
    return 1
  end
  if pttl == -2 or pttl <= 0 then
    redis.call('SET', key, '1', 'PX', required_ms)
    return 1
  end
  if pttl < required_ms then
    redis.call('SET', key, '1', 'PX', required_ms)
  end
  return 1
end

local function apply_session_revocation(key, required_ms)
  local kind = redis_type(key)
  if kind == 'none' then
    redis.call('SET', key, '1', 'PX', required_ms)
    return 1
  end
  if kind ~= 'string' then
    return 0
  end
  local value = redis.call('GET', key)
  if is_grace(value) then
    redis.call('SET', key, '1', 'PX', required_ms)
    return 1
  end
  if value ~= '1' then
    return 0
  end
  local pttl = redis.call('PTTL', key)
  if pttl == -1 then
    return 1
  end
  if pttl == -2 or pttl <= 0 then
    redis.call('SET', key, '1', 'PX', required_ms)
    return 1
  end
  if pttl < required_ms then
    redis.call('SET', key, '1', 'PX', required_ms)
  end
  return 1
end
"""

_ROTATE_REFRESH_LUA = (
    f"-- {ROTATE_REFRESH_LUA_TAG}\n"
    + _REDIS_REVOCATION_LIB_LUA
    + r"""
local operation_s = tonumber(ARGV[4])
local max_access_ms = tonumber(ARGV[5])
if operation_s == nil or operation_s < 1 or max_access_ms == nil or max_access_ms < 1 then
  return {-2, ''}
end
local required_ms = math.max(max_access_ms, math.floor(operation_s * 1000))

local current = redis.call('GET', KEYS[1])
if not current then
  if apply_session_revocation(KEYS[2], required_ms) == 0 then
    return {-2, ''}
  end
  redis.call('DEL', KEYS[1])
  return {0, ''}
end

if current == ARGV[1] then
  local session_kind = redis_type(KEYS[2])
  if session_kind ~= 'none' then
    if session_kind ~= 'string' then
      return {-2, ''}
    end
    local existing = redis.call('GET', KEYS[2])
    if existing == '1' then
      if apply_session_revocation(KEYS[2], required_ms) == 0 then
        return {-2, ''}
      end
      redis.call('DEL', KEYS[1])
      return {-1, ''}
    end
    if not is_grace(existing) then
      return {-2, ''}
    end
  end
  local grace = 'grace\n' .. ARGV[1] .. '\n' .. ARGV[2]
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  redis.call('SET', KEYS[2], grace, 'EX', __REFRESH_GRACE_SECONDS__)
  return {1, ARGV[2]}
end

local state = redis.call('GET', KEYS[2])
if state then
  local first = string.find(state, '\n', 1, true)
  local second = first and string.find(state, '\n', first + 1, true) or nil
  if first and second and string.sub(state, 1, first - 1) == 'grace' then
    local previous_binding = string.sub(state, first + 1, second - 1)
    local replacement_binding = string.sub(state, second + 1)
    if current == replacement_binding and ARGV[1] == previous_binding
        and replacement_binding ~= '' then
      return {2, replacement_binding}
    end
  end
end

if apply_session_revocation(KEYS[2], required_ms) == 0 then
  return {-2, ''}
end
redis.call('DEL', KEYS[1])
return {-1, ''}
"""
).replace("__REFRESH_GRACE_SECONDS__", str(REFRESH_GRACE_SECONDS))

_REVOKE_SESSION_LUA = (
    f"-- {REVOKE_SESSION_LUA_TAG}\n"
    + _REDIS_REVOCATION_LIB_LUA
    + r"""
local jti_required = tonumber(ARGV[1])
local session_requested = tonumber(ARGV[2])
local max_access = tonumber(ARGV[3])
if jti_required == nil or session_requested == nil or max_access == nil then
  return 0
end
if jti_required < 1 or session_requested < 1 or max_access < 1 then
  return 0
end
local session_required = math.max(max_access, session_requested)
if apply_jti_revocation(KEYS[1], jti_required) == 0 then
  return 0
end
if apply_session_revocation(KEYS[2], session_required) == 0 then
  return 0
end
redis.call('DEL', KEYS[3])
return 1
"""
)


def _grace_bindings(state: object) -> tuple[str, str] | None:
    if isinstance(state, bytes):
        try:
            state = state.decode("utf-8")
        except UnicodeError:
            return None
    if not isinstance(state, str):
        return None
    parts = state.split("\n", 2)
    if len(parts) != 3 or parts[0] != "grace" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _is_revocation_marker(value: object) -> bool:
    return value in {"1", b"1"}


class _JwtMemoryState:
    """与撤销/轮换 Lua 相同的内存 TTL 语义，供测试替身复用。"""

    def __init__(
        self,
        values: dict[str, Any],
        expire_at_ms: dict[str, int] | None,
        key_types: dict[str, str] | None,
        now_ms: int,
        *,
        track_ttl: bool,
    ) -> None:
        self.values = values
        self.expire_at_ms = expire_at_ms if expire_at_ms is not None else {}
        self.key_types = key_types if key_types is not None else {}
        self.now_ms = now_ms
        self.track_ttl = track_ttl
        if self.track_ttl:
            self.purge()

    def purge(self) -> None:
        expired = [key for key, deadline in self.expire_at_ms.items() if deadline <= self.now_ms]
        for key in expired:
            self.values.pop(key, None)
            self.expire_at_ms.pop(key, None)
            self.key_types.pop(key, None)

    def redis_type(self, key: str) -> str:
        if self.track_ttl:
            self.purge()
        if key not in self.values:
            return "none"
        if key in self.key_types:
            return self.key_types[key]
        value = self.values[key]
        if isinstance(value, (str, bytes)):
            return "string"
        return "hash"

    def pttl(self, key: str) -> int:
        if self.track_ttl:
            self.purge()
        if key not in self.values:
            return -2
        if not self.track_ttl or key not in self.expire_at_ms:
            return -1
        return max(0, self.expire_at_ms[key] - self.now_ms)

    def set_px(self, key: str, value: str, px: int) -> None:
        self.values[key] = value
        self.key_types[key] = "string"
        if self.track_ttl:
            self.expire_at_ms[key] = self.now_ms + max(1, px)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.expire_at_ms.pop(key, None)
        self.key_types.pop(key, None)

    def get(self, key: str) -> object | None:
        if self.track_ttl:
            self.purge()
        return self.values.get(key)


def _apply_jti_revocation(state: _JwtMemoryState, key: str, required_ms: int) -> bool:
    kind = state.redis_type(key)
    if kind == "none":
        state.set_px(key, "1", required_ms)
        return True
    if kind != "string":
        return False
    value = state.get(key)
    if not _is_revocation_marker(value):
        return False
    pttl = state.pttl(key)
    if pttl == -1:
        return True
    if pttl == -2 or pttl <= 0:
        state.set_px(key, "1", required_ms)
        return True
    if pttl < required_ms:
        state.set_px(key, "1", required_ms)
    return True


def _apply_session_revocation(state: _JwtMemoryState, key: str, required_ms: int) -> bool:
    kind = state.redis_type(key)
    if kind == "none":
        state.set_px(key, "1", required_ms)
        return True
    if kind != "string":
        return False
    value = state.get(key)
    if _grace_bindings(value) is not None:
        state.set_px(key, "1", required_ms)
        return True
    if not _is_revocation_marker(value):
        return False
    pttl = state.pttl(key)
    if pttl == -1:
        return True
    if pttl == -2 or pttl <= 0:
        state.set_px(key, "1", required_ms)
        return True
    if pttl < required_ms:
        state.set_px(key, "1", required_ms)
    return True


def eval_memory_jwt_script(
    values: dict[str, Any],
    script: str,
    args: tuple[object, ...],
    *,
    expire_at_ms: dict[str, int] | None = None,
    key_types: dict[str, str] | None = None,
    now_ms: int | None = None,
) -> object | None:
    """复用撤销/轮换 Lua 的内存语义；非 JWT 脚本返回 None。"""

    if REVOKE_SESSION_LUA_TAG not in script and ROTATE_REFRESH_LUA_TAG not in script:
        return None
    state = _JwtMemoryState(
        values,
        expire_at_ms,
        key_types,
        0 if now_ms is None else now_ms,
        track_ttl=expire_at_ms is not None,
    )
    if REVOKE_SESSION_LUA_TAG in script:
        if len(args) < 6:
            return 0
        jti_key, session_key, family_key = map(str, args[:3])
        try:
            jti_required = int(str(args[3]))
            session_requested = int(str(args[4]))
            max_access = int(str(args[5]))
        except (TypeError, ValueError):
            return 0
        if jti_required < 1 or session_requested < 1 or max_access < 1:
            return 0
        session_required = max(max_access, session_requested)
        if not _apply_jti_revocation(state, jti_key, jti_required):
            return 0
        if not _apply_session_revocation(state, session_key, session_required):
            return 0
        state.delete(family_key)
        return 1
    if len(args) < 7:
        return [-2, ""]
    family_key, session_key = map(str, args[:2])
    expected, replacement = str(args[2]), str(args[3])
    try:
        family_ttl_s = int(str(args[4]))
        operation_s = int(str(args[5]))
        max_access_ms = int(str(args[6]))
    except (TypeError, ValueError):
        return [-2, ""]
    if family_ttl_s < 1 or operation_s < 1 or max_access_ms < 1:
        return [-2, ""]
    required_ms = max(max_access_ms, operation_s * 1000)
    current = state.get(family_key)
    if current is None:
        if not _apply_session_revocation(state, session_key, required_ms):
            return [-2, ""]
        state.delete(family_key)
        return [0, ""]
    if current == expected:
        kind = state.redis_type(session_key)
        if kind != "none":
            if kind != "string":
                return [-2, ""]
            existing = state.get(session_key)
            if _is_revocation_marker(existing):
                if not _apply_session_revocation(state, session_key, required_ms):
                    return [-2, ""]
                state.delete(family_key)
                return [-1, ""]
            if _grace_bindings(existing) is None:
                return [-2, ""]
        state.set_px(family_key, replacement, family_ttl_s * 1000)
        state.set_px(
            session_key,
            f"grace\n{expected}\n{replacement}",
            REFRESH_GRACE_SECONDS * 1000,
        )
        return [1, replacement]
    existing = state.get(session_key)
    bindings = _grace_bindings(existing)
    if (
        bindings is not None
        and current == bindings[1]
        and expected == bindings[0]
        and bindings[1]
    ):
        return [2, bindings[1]]
    if not _apply_session_revocation(state, session_key, required_ms):
        return [-2, ""]
    state.delete(family_key)
    return [-1, ""]
