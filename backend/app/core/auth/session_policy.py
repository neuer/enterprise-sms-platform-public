"""AD 会话策略：PostgreSQL 权威 revision，Redis 只接受单调 CAS。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.observability import (
    observe_session_policy_conflict,
    observe_session_policy_publish,
    observe_session_policy_revisions,
    observe_session_policy_snapshot_age,
)

AD_SESSION_POLICY_KEY = "auth:ad:session-policy"
MIN_AD_SESSION_MAX_AGE_MINUTES = 15
MAX_AD_SESSION_MAX_AGE_MINUTES = 10_080

POLICY_CAS_LUA = r"""
-- auth-session-policy-cas-v1
local function redis_type(key)
  local current = redis.call('TYPE', key)
  if type(current) == 'table' then
    return current['ok']
  end
  return current
end

local function fields_from_string(raw)
  if not raw then
    return nil
  end
  local sep = string.find(raw, ':', 1, true)
  if not sep then
    return nil
  end
  return {
    revision = string.sub(raw, 1, sep - 1),
    ad_session_max_age_minutes = string.sub(raw, sep + 1),
    updated_at_epoch = '0',
  }
end

local function current_fields(key)
  local kind = redis_type(key)
  if kind == 'hash' then
    local current = redis.call('HGETALL', key)
    if #current == 0 then
      return nil
    end
    local fields = {}
    for i = 1, #current, 2 do
      fields[current[i]] = current[i + 1]
    end
    return fields
  end
  if kind == 'string' then
    return fields_from_string(redis.call('GET', key))
  end
  return nil
end

local function write_hash(key, revision, minutes, epoch)
  redis.call('DEL', key)
  redis.call('HSET', key,
    'revision', revision,
    'ad_session_max_age_minutes', minutes,
    'updated_at_epoch', epoch)
  redis.call('PERSIST', key)
end

local incoming_revision = tonumber(ARGV[1])
local incoming_minutes = ARGV[2]
local incoming_epoch = ARGV[3]
if incoming_revision == nil or incoming_revision < 1 then
  return {0, 'invalid'}
end

local fields = current_fields(KEYS[1])
if fields == nil then
  write_hash(KEYS[1], ARGV[1], incoming_minutes, incoming_epoch)
  return {1, 'accepted'}
end

local current_revision = tonumber(fields['revision'])
if current_revision == nil then
  return {0, 'conflict'}
end
if incoming_revision > current_revision then
  write_hash(KEYS[1], ARGV[1], incoming_minutes, incoming_epoch)
  return {1, 'accepted'}
end
if incoming_revision == current_revision then
  if fields['ad_session_max_age_minutes'] == incoming_minutes then
    return {1, 'idempotent'}
  end
  return {0, 'conflict'}
end
return {0, 'stale'}
"""

POLICY_LOAD_LUA = r"""
-- auth-session-policy-load-v1
local function redis_type(key)
  local current = redis.call('TYPE', key)
  if type(current) == 'table' then
    return current['ok']
  end
  return current
end

local kind = redis_type(KEYS[1])
if kind == 'hash' then
  return redis.call('HGETALL', KEYS[1])
end
if kind == 'string' then
  return redis.call('GET', KEYS[1])
end
return false
"""


class AuthSessionPolicyConflict(SessionStateUnavailable):
    """同 revision 内容冲突、旧 revision 回滚或 Redis 策略状态不可用。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"AD session policy {reason}")


@dataclass(frozen=True, slots=True)
class AuthSessionPolicy:
    """认证热路径只消费这个最小快照，不解析完整 sys_config。"""

    revision: int
    ad_session_max_age_minutes: int
    updated_at_epoch: int = 0

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("AD session policy revision is invalid")
        if not (
            MIN_AD_SESSION_MAX_AGE_MINUTES
            <= self.ad_session_max_age_minutes
            <= MAX_AD_SESSION_MAX_AGE_MINUTES
        ):
            raise ValueError("AD session policy max age is invalid")


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _mapping(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        return {_text(key): _text(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)) and raw and len(raw) % 2 == 0:
        return {_text(raw[index]): _text(raw[index + 1]) for index in range(0, len(raw), 2)}
    raise SessionStateUnavailable("AD session policy unavailable")


def parse_auth_session_policy(raw: object) -> AuthSessionPolicy:
    """解析 Redis HASH 或迁移期 `revision:minutes` 旧字符串。"""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str) and ":" in raw and not raw.startswith("{"):
        revision_text, minutes_text = raw.split(":", 1)
        return AuthSessionPolicy(int(revision_text), int(minutes_text))
    mapping = _mapping(raw)
    return AuthSessionPolicy(
        int(mapping["revision"]),
        int(mapping["ad_session_max_age_minutes"]),
        int(mapping.get("updated_at_epoch") or 0),
    )


def effective_ad_deadline(
    auth_time: float,
    token_deadline: float,
    policy: AuthSessionPolicy,
) -> float:
    """缩短立即收紧既有会话；延长不改已签发 deadline。"""

    return min(token_deadline, auth_time + policy.ad_session_max_age_minutes * 60)


async def load_auth_session_policy(store: Any) -> AuthSessionPolicy:
    """读取当前策略；缺失、畸形或冲突一律失败关闭。"""

    eval_script = getattr(store, "eval", None)
    raw: object
    try:
        if eval_script is not None:
            raw = await eval_script(POLICY_LOAD_LUA, 1, AD_SESSION_POLICY_KEY)
        else:
            raw = await store.get(AD_SESSION_POLICY_KEY)
    except SessionStateUnavailable:
        raise
    except Exception:
        raise SessionStateUnavailable("AD session policy unavailable") from None
    if raw is None or raw is False or raw == "" or raw == b"" or raw == [] or raw == {}:
        raise SessionStateUnavailable("AD session policy unavailable")
    try:
        policy = parse_auth_session_policy(raw)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise SessionStateUnavailable("AD session policy unavailable") from None
    observe_session_policy_revisions(redis_revision=policy.revision)
    if policy.updated_at_epoch > 0:
        observe_session_policy_snapshot_age(policy.updated_at_epoch)
    return policy


def _cas_result(value: object) -> tuple[int, str]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), _text(value[1])
        except (TypeError, ValueError, UnicodeError):
            raise SessionStateUnavailable("AD session policy unavailable") from None
    raise SessionStateUnavailable("AD session policy unavailable")


def eval_memory_session_policy(
    values: dict[str, Any],
    script: str,
    args: tuple[object, ...],
) -> object | None:
    """与 Lua 相同的内存语义，供测试 Fake Redis 复用。"""

    if "auth-session-policy-cas-v1" in script:
        incoming = AuthSessionPolicy(
            int(_text(args[1])),
            int(_text(args[2])),
            int(_text(args[3])),
        )
        key = str(args[0])
        current_raw = values.get(key)
        current = None
        if current_raw is not None:
            try:
                current = parse_auth_session_policy(current_raw)
            except (SessionStateUnavailable, KeyError, TypeError, ValueError, UnicodeError):
                return [0, "conflict"]
        try:
            outcome = apply_policy_cas(current, incoming)
        except AuthSessionPolicyConflict as error:
            return [0, error.reason]
        if outcome == "accepted":
            values[key] = {
                "revision": incoming.revision,
                "ad_session_max_age_minutes": incoming.ad_session_max_age_minutes,
                "updated_at_epoch": incoming.updated_at_epoch,
            }
        return [1, outcome]
    if "auth-session-policy-load-v1" in script:
        return values.get(str(args[0]))
    return None


def apply_policy_cas(
    current: AuthSessionPolicy | None,
    incoming: AuthSessionPolicy,
) -> str:
    """与 Lua 相同的比较规则，供无 Redis 的单测复用。"""

    if current is None:
        return "accepted"
    if incoming.revision > current.revision:
        return "accepted"
    if (
        incoming.revision == current.revision
        and incoming.ad_session_max_age_minutes == current.ad_session_max_age_minutes
    ):
        return "idempotent"
    if incoming.revision == current.revision:
        raise AuthSessionPolicyConflict("conflict")
    raise AuthSessionPolicyConflict("stale")


def compare_authoritative_policy(
    postgres: AuthSessionPolicy,
    redis: AuthSessionPolicy | None,
) -> str:
    """比较权威行与 Redis 快照；超前或同版本冲突必须失败关闭。"""

    observe_session_policy_revisions(
        postgres_revision=postgres.revision,
        redis_revision=None if redis is None else redis.revision,
    )
    if redis is None:
        return "missing"
    if redis.revision < postgres.revision:
        return "behind"
    if redis.revision > postgres.revision:
        observe_session_policy_conflict("ahead")
        return "ahead"
    if redis.ad_session_max_age_minutes != postgres.ad_session_max_age_minutes:
        observe_session_policy_conflict("mismatch")
        return "conflict"
    return "aligned"


async def publish_auth_session_policy(store: Any, policy: AuthSessionPolicy) -> str:
    """原子发布更高 revision；旧 revision 不能覆盖新值。"""

    eval_script = getattr(store, "eval", None)
    if eval_script is None:
        raise SessionStateUnavailable("AD session policy unavailable")
    try:
        raw = await eval_script(
            POLICY_CAS_LUA,
            1,
            AD_SESSION_POLICY_KEY,
            str(policy.revision),
            str(policy.ad_session_max_age_minutes),
            str(policy.updated_at_epoch),
        )
    except SessionStateUnavailable:
        observe_session_policy_publish("unavailable")
        raise
    except Exception:
        observe_session_policy_publish("unavailable")
        raise SessionStateUnavailable("AD session policy unavailable") from None
    status, outcome = _cas_result(raw)
    if status == 1:
        observe_session_policy_publish(outcome)
        observe_session_policy_revisions(redis_revision=policy.revision)
        return outcome
    observe_session_policy_publish(outcome)
    observe_session_policy_conflict(outcome)
    raise AuthSessionPolicyConflict(outcome)
