"""用真实 Lua 解释 Redis EVAL；时间可注入。无 lua 时回退同语义端口。"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

_LUA_BINARIES = ("lua", "lua5.5", "lua5.4", "lua5.3", "lua5.1")
_SHIM = r"""
local now_sec = tonumber(os.getenv('REDIS_TIME_SEC'))
local decoded = (loadstring or load)(io.read('*a'), 'payload')()
local hashes = {}
local kinds = {}
local expires = {}
for key, fields in pairs(decoded.hashes) do
  hashes[key] = {}
  for field, value in pairs(fields) do
    hashes[key][field] = tostring(value)
  end
end
for key, kind in pairs(decoded.kinds) do
  kinds[key] = kind
end
KEYS = decoded.keys
ARGV = decoded.args
redis = {}
local function as_type(key)
  return kinds[key] or 'none'
end
function redis.call(cmd, ...)
  cmd = string.upper(tostring(cmd))
  local args = {...}
  if cmd == 'TIME' then
    return {tostring(now_sec), '0'}
  end
  if cmd == 'TYPE' then
    return {ok = as_type(tostring(args[1]))}
  end
  local key = tostring(args[1])
  if cmd == 'HGET' then
    if as_type(key) == 'none' then
      return false
    end
    if as_type(key) ~= 'hash' then
      error('WRONGTYPE')
    end
    local value = hashes[key] and hashes[key][tostring(args[2])]
    if value == nil then
      return false
    end
    return value
  end
  if cmd == 'HSET' then
    kinds[key] = 'hash'
    hashes[key] = hashes[key] or {}
    hashes[key][tostring(args[2])] = tostring(args[3])
    return 1
  end
  if cmd == 'HINCRBY' then
    kinds[key] = 'hash'
    hashes[key] = hashes[key] or {}
    local field = tostring(args[2])
    local current = tonumber(hashes[key][field] or '0') or 0
    local next_value = current + tonumber(args[3])
    hashes[key][field] = tostring(next_value)
    return next_value
  end
  if cmd == 'EXPIRE' then
    expires[key] = tonumber(args[2])
    return 1
  end
  if cmd == 'HDEL' then
    if as_type(key) == 'none' then
      return 0
    end
    if as_type(key) ~= 'hash' then
      error('WRONGTYPE')
    end
    local field = tostring(args[2])
    if hashes[key] and hashes[key][field] ~= nil then
      hashes[key][field] = nil
      return 1
    end
    return 0
  end
  error('unsupported redis command ' .. cmd)
end
local runner = load(decoded.script)
if runner == nil and loadstring ~= nil then
  runner = loadstring(decoded.script)
end
local result = runner()
local function encode(value)
  if value == nil or value == false then
    return 'null'
  end
  local kind = type(value)
  if kind == 'number' then
    if value ~= value or value == math.huge or value == -math.huge then
      error('non-finite number')
    end
    return tostring(value)
  end
  if kind == 'string' then
    return string.format('%q', value)
  end
  if kind == 'table' then
    local parts = {}
    for index, item in ipairs(value) do
      parts[index] = encode(item)
    end
    return '[' .. table.concat(parts, ',') .. ']'
  end
  error('unsupported return ' .. kind)
end
io.write('{"hashes":{')
local first = true
for key, fields in pairs(hashes) do
  if not first then io.write(',') end
  first = false
  io.write(string.format('%q:{', key))
  local inner = true
  for field, value in pairs(fields) do
    if not inner then io.write(',') end
    inner = false
    io.write(string.format('%q:%q', field, value))
  end
  io.write('}')
end
io.write('},"kinds":{')
first = true
for key, kind in pairs(kinds) do
  if not first then io.write(',') end
  first = false
  io.write(string.format('%q:%q', key, kind))
end
io.write('},"expires":{')
first = true
for key, ttl in pairs(expires) do
  if not first then io.write(',') end
  first = false
  io.write(string.format('%q:%s', key, tostring(ttl)))
end
io.write('},"result":')
io.write(encode(result))
io.write('}')
"""


def lua_binary() -> str | None:
    for name in _LUA_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _lua_literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        parts = [
            f"[{_lua_literal(key)}]={_lua_literal(item)}" for key, item in value.items()
        ]
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(_lua_literal(item) for item in value) + "}"
    raise TypeError(f"unsupported lua literal {type(value)!r}")


class LuaRedis:
    """执行真实 Lua 脚本；`eval` 为同步，`async_eval` 供 ApplicationRateLimiter。"""

    def __init__(self, now_sec: int) -> None:
        self.now_sec = now_sec
        self.hashes: dict[str, dict[str, str]] = {}
        self.kinds: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.deleted: list[str] = []
        self.calls: list[tuple[object, ...]] = []
        self._lua = lua_binary()

    def seed_hash(self, key: str, fields: Mapping[str, str]) -> None:
        self.kinds[key] = "hash"
        self.hashes[key] = {str(name): str(value) for name, value in fields.items()}

    def seed_string(self, key: str, value: str) -> None:
        self.kinds[key] = "string"
        self.hashes[key] = {"_string": value}

    def hgetall(self, key: str) -> dict[str, str]:
        if self.kinds.get(key) != "hash":
            return {}
        return dict(self.hashes.get(key, {}))

    def time(self) -> tuple[int, int]:
        return self.now_sec, 0

    def eval(self, script: object, numkeys: object, *args: object) -> Any:
        self.calls.append((script, numkeys, *args))
        count = int(numkeys)
        keys = [str(item) for item in args[:count]]
        argv = [str(item) for item in args[count:]]
        if self._lua is None:
            from tests.support.lua_redis_port import eval_lua_subset

            hashes, kinds, expires, result = eval_lua_subset(
                str(script),
                keys,
                argv,
                now_sec=self.now_sec,
                hashes=self.hashes,
                kinds=self.kinds,
            )
            self.hashes = hashes
            self.kinds = kinds
            self.expires = expires
            return result
        payload = (
            "return {script="
            + _lua_literal(str(script))
            + ",keys="
            + _lua_literal(keys)
            + ",args="
            + _lua_literal(argv)
            + ",hashes="
            + _lua_literal(self.hashes)
            + ",kinds="
            + _lua_literal(self.kinds)
            + "}"
        )
        completed = subprocess.run(
            [self._lua, "-e", _SHIM],
            check=False,
            capture_output=True,
            text=True,
            input=payload,
            env={**__import__("os").environ, "REDIS_TIME_SEC": str(self.now_sec)},
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or "lua eval failed")
        document = json.loads(completed.stdout)
        self.hashes = {
            str(key): {str(field): str(value) for field, value in fields.items()}
            for key, fields in document["hashes"].items()
        }
        self.kinds = {str(key): str(kind) for key, kind in document["kinds"].items()}
        self.expires = {
            str(key): int(ttl) for key, ttl in document.get("expires", {}).items()
        }
        return document["result"]

    async def async_eval(self, script: object, numkeys: object, *args: object) -> Any:
        return self.eval(script, numkeys, *args)


class AsyncLuaRedis:
    """把同步 LuaRedis 暴露为 ApplicationRateLimiter 所需的 async eval。"""

    def __init__(self, redis: LuaRedis) -> None:
        self.redis = redis

    async def eval(self, script: object, numkeys: object, *args: object) -> Any:
        return self.redis.eval(script, numkeys, *args)
