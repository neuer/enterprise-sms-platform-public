"""应用成本限流 v1→v2 的受控切换状态机。

方案 A：先关闭新发送入口并用部署/进程控制隔离旧 writer，确认在途受理结束后
才写入 fence；not_before 只由 Redis TIME + 旧窗口 + 明确安全裕量计算。
migration generation/state 只由本入口更新，业务请求不得自我宣布完成。

静态 v1 继承只在 old writers 已冻结后有效；max(v1,v2) 不是混跑并集保证。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.services.app_ratelimit import (
    COST_WINDOW_SECONDS,
    WRITER_CUTOVER_MARKER_KEY,
)

CUTOVER_MARKER_KEY = WRITER_CUTOVER_MARKER_KEY
CUTOVER_SCHEMA_VERSION = 1
WRITER_PROTOCOL_VERSION = 2
CUTOVER_SAFETY_MARGIN_SECONDS = 5
CUTOVER_ADMISSION_REASON = "writer_cutover"
WRITER_PROTOCOL_RELATIVE = "deploy/writer-protocol.json"

CUTOVER_STATES = (
    "preparing",
    "old_writers_fenced",
    "waiting_window",
    "active_v2",
    "aborted_closed",
)
CutoverState = Literal[
    "preparing",
    "old_writers_fenced",
    "waiting_window",
    "active_v2",
    "aborted_closed",
]
ProbeStatus = Literal["absent", "present", "timeout", "error"]

MARKER_FIELDS = (
    "schema_version",
    "generation",
    "target_writer_version",
    "minimum_writer_version",
    "fence_time",
    "not_before",
    "state",
    "release_binding",
    "admission_reason",
    "window_seconds",
    "safety_margin_seconds",
)

CUTOVER_CAS_LUA = """
local marker = KEYS[1]
local action = ARGV[1]
local expect_generation = ARGV[2]
local expect_state = ARGV[3]
local release_binding = ARGV[4]
local target_writer = ARGV[5]
local min_writer = ARGV[6]
local admission_reason = ARGV[7]
local window_seconds = ARGV[8]
local safety_margin = ARGV[9]
local t = redis.call('TIME')
local now_sec = tonumber(t[1])
if now_sec == nil then
  return {-1, '', '', ''}
end
local function redis_type(key)
  local typ = redis.call('TYPE', key)
  if type(typ) == 'table' and typ.ok ~= nil then
    return typ.ok
  end
  return typ
end
local function field(name)
  local raw = redis.call('HGET', marker, name)
  if raw == false then
    return ''
  end
  return tostring(raw)
end
local typ = redis_type(marker)
if typ ~= 'none' and typ ~= 'hash' then
  return {-2, '', '', tostring(now_sec)}
end
local exists = typ == 'hash'
local current_generation = field('generation')
local current_state = field('state')
if exists then
  if field('schema_version') ~= '1' then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if current_generation == '' or current_state == '' then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if tonumber(current_generation) == nil or tonumber(current_generation) < 1 then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if current_state ~= 'preparing' and current_state ~= 'old_writers_fenced'
      and current_state ~= 'waiting_window' and current_state ~= 'active_v2'
      and current_state ~= 'aborted_closed' then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if action == 'bootstrap' and current_state == 'active_v2' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  local bound = field('release_binding')
  if action ~= 'takeover_prepare' and bound ~= '' and bound ~= release_binding then
    return {-6, current_state, current_generation, tostring(now_sec)}
  end
end
if expect_generation ~= '' then
  if (not exists) or current_generation ~= expect_generation then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
end
if expect_state ~= '' then
  if (not exists) or current_state ~= expect_state then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
end
local function write_fields(generation, state, fence_time, not_before, minimum)
  redis.call('HSET', marker, 'schema_version', '1')
  redis.call('HSET', marker, 'generation', generation)
  redis.call('HSET', marker, 'target_writer_version', target_writer)
  redis.call('HSET', marker, 'minimum_writer_version', minimum)
  redis.call('HSET', marker, 'fence_time', fence_time)
  redis.call('HSET', marker, 'not_before', not_before)
  redis.call('HSET', marker, 'state', state)
  redis.call('HSET', marker, 'release_binding', release_binding)
  redis.call('HSET', marker, 'admission_reason', admission_reason)
  redis.call('HSET', marker, 'window_seconds', window_seconds)
  redis.call('HSET', marker, 'safety_margin_seconds', safety_margin)
end
if action == 'takeover_prepare' then
  if (not exists) or current_state ~= 'preparing' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local generation = tostring(tonumber(current_generation) + 1)
  write_fields(generation, 'preparing', '', '', min_writer)
  return {1, 'preparing', generation, tostring(now_sec)}
end
if action == 'prepare' then
  if exists and current_state == 'active_v2'
      and field('target_writer_version') == target_writer then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if exists and (
      current_state == 'preparing'
      or current_state == 'old_writers_fenced'
      or current_state == 'waiting_window'
    ) then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  local generation = '1'
  if exists then
    generation = tostring(tonumber(current_generation))
    if current_state == 'aborted_closed' then
      generation = tostring(tonumber(current_generation) + 1)
    else
      return {0, current_state, current_generation, tostring(now_sec)}
    end
  end
  write_fields(generation, 'preparing', '', '', min_writer)
  return {1, 'preparing', generation, tostring(now_sec)}
end
if action == 'fence' then
  if exists and current_state == 'old_writers_fenced' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if exists and current_state == 'waiting_window' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if exists and current_state == 'active_v2' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if (not exists) or current_state ~= 'preparing' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local window = tonumber(window_seconds)
  local margin = tonumber(safety_margin)
  if window == nil or margin == nil or window < 1 or margin < 0 then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  local not_before = now_sec + window + margin
  write_fields(
    current_generation,
    'old_writers_fenced',
    tostring(now_sec),
    tostring(not_before),
    min_writer
  )
  return {1, 'old_writers_fenced', current_generation, tostring(now_sec)}
end
if action == 'wait' then
  if exists and current_state == 'waiting_window' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if exists and current_state == 'active_v2' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if (not exists) or current_state ~= 'old_writers_fenced' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local fence_time = tonumber(field('fence_time'))
  if fence_time == nil then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if now_sec < fence_time then
    return {-5, current_state, current_generation, tostring(now_sec)}
  end
  write_fields(
    current_generation,
    'waiting_window',
    field('fence_time'),
    field('not_before'),
    field('minimum_writer_version')
  )
  return {1, 'waiting_window', current_generation, tostring(now_sec)}
end
if action == 'activate' then
  if exists and current_state == 'active_v2' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if (not exists) or current_state ~= 'waiting_window' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local fence_time = tonumber(field('fence_time'))
  local not_before = tonumber(field('not_before'))
  if fence_time == nil or not_before == nil then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if now_sec < fence_time then
    return {-5, current_state, current_generation, tostring(now_sec)}
  end
  if now_sec < not_before then
    return {-3, current_state, current_generation, tostring(now_sec)}
  end
  write_fields(
    current_generation,
    'active_v2',
    field('fence_time'),
    field('not_before'),
    target_writer
  )
  return {1, 'active_v2', current_generation, tostring(now_sec)}
end
if action == 'abort' then
  local generation = current_generation
  if not exists then
    generation = '1'
  end
  write_fields(generation, 'aborted_closed', field('fence_time'), field('not_before'), min_writer)
  return {1, 'aborted_closed', generation, tostring(now_sec)}
end
if action == 'rollback_prepare' then
  if exists and (
      current_state == 'preparing'
      or current_state == 'old_writers_fenced'
      or current_state == 'waiting_window'
    ) then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if (not exists) or current_state ~= 'active_v2' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local generation = tostring(tonumber(current_generation) + 1)
  write_fields(generation, 'preparing', '', '', min_writer)
  return {1, 'preparing', generation, tostring(now_sec)}
end
if action == 'rollback_finish' then
  if exists and current_state == 'preparing'
      and field('target_writer_version') == target_writer then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if (not exists) or current_state ~= 'waiting_window' then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  local fence_time = tonumber(field('fence_time'))
  local not_before = tonumber(field('not_before'))
  if fence_time == nil or not_before == nil then
    return {-2, current_state, current_generation, tostring(now_sec)}
  end
  if now_sec < fence_time then
    return {-5, current_state, current_generation, tostring(now_sec)}
  end
  if now_sec < not_before then
    return {-3, current_state, current_generation, tostring(now_sec)}
  end
  write_fields(
    current_generation,
    'preparing',
    field('fence_time'),
    field('not_before'),
    target_writer
  )
  return {1, 'preparing', current_generation, tostring(now_sec)}
end
if action == 'bootstrap' then
  if exists and current_state == 'active_v2' then
    return {1, current_state, current_generation, tostring(now_sec)}
  end
  if exists then
    return {0, current_state, current_generation, tostring(now_sec)}
  end
  write_fields('1', 'active_v2', tostring(now_sec), tostring(now_sec), target_writer)
  return {1, 'active_v2', '1', tostring(now_sec)}
end
return {-4, current_state, current_generation, tostring(now_sec)}
"""


class CutoverError(RuntimeError):
    """切换状态机失败关闭。"""


class CutoverRedis(Protocol):
    def eval(self, script: object, numkeys: object, *args: object) -> Any: ...

    def hgetall(self, key: str) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class CutoverMarker:
    schema_version: int
    generation: int
    target_writer_version: int
    minimum_writer_version: int
    fence_time: int | None
    not_before: int | None
    state: CutoverState
    release_binding: str
    admission_reason: str
    window_seconds: int
    safety_margin_seconds: int

    def as_hash(self) -> dict[str, str]:
        return {
            "schema_version": str(self.schema_version),
            "generation": str(self.generation),
            "target_writer_version": str(self.target_writer_version),
            "minimum_writer_version": str(self.minimum_writer_version),
            "fence_time": "" if self.fence_time is None else str(self.fence_time),
            "not_before": "" if self.not_before is None else str(self.not_before),
            "state": self.state,
            "release_binding": self.release_binding,
            "admission_reason": self.admission_reason,
            "window_seconds": str(self.window_seconds),
            "safety_margin_seconds": str(self.safety_margin_seconds),
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    detail: str = ""

    @property
    def confirmed_absent(self) -> bool:
        return self.status == "absent"


@dataclass(frozen=True, slots=True)
class AdmissionView:
    state: str
    reason: str
    owned: bool


@dataclass(frozen=True, slots=True)
class CutoverResult:
    ok: bool
    state: CutoverState | Literal[""]
    generation: int
    redis_time: int
    admission_state: str
    admission_reason: str
    opened: bool
    error: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "state": self.state,
            "generation": self.generation,
            "redis_time": self.redis_time,
            "admission_state": self.admission_state,
            "admission_reason": self.admission_reason,
            "opened": self.opened,
            "error": self.error,
        }


class WriterCutoverExecutor(Protocol):
    def close_admission(self, *, reason: str, generation: int) -> AdmissionView: ...

    def query_admission(self) -> AdmissionView: ...

    def open_admission_if_owned(self, *, reason: str) -> AdmissionView: ...

    def isolate_writers(self) -> ProbeResult: ...

    def probe_writers(self) -> ProbeResult: ...

    def probe_in_flight_accepts(self) -> ProbeResult: ...

    def trusted_writer_version(self, root: Path) -> int: ...


def _optional_int(raw: str, name: str) -> int | None:
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise CutoverError(f"cutover marker {name} is corrupt") from exc
    if value < 0:
        raise CutoverError(f"cutover marker {name} is corrupt")
    return value


def _required_int(raw: str, name: str, *, minimum: int = 1) -> int:
    value = _optional_int(raw, name)
    if value is None or value < minimum:
        raise CutoverError(f"cutover marker {name} is corrupt")
    return value


def parse_cutover_marker(fields: Mapping[str, str]) -> CutoverMarker:
    """解析全局切换 marker；字段缺失、多余或损坏时失败关闭。"""

    if set(fields) != set(MARKER_FIELDS):
        raise CutoverError("cutover marker fields are corrupt")
    values = {name: str(fields[name]) for name in MARKER_FIELDS}
    state = values["state"]
    if state not in CUTOVER_STATES:
        raise CutoverError("cutover marker state is corrupt")
    return CutoverMarker(
        schema_version=_required_int(values["schema_version"], "schema_version"),
        generation=_required_int(values["generation"], "generation"),
        target_writer_version=_required_int(
            values["target_writer_version"], "target_writer_version"
        ),
        minimum_writer_version=_required_int(
            values["minimum_writer_version"],
            "minimum_writer_version",
            minimum=1,
        ),
        fence_time=_optional_int(values["fence_time"], "fence_time"),
        not_before=_optional_int(values["not_before"], "not_before"),
        state=state,  # type: ignore[arg-type]
        release_binding=values["release_binding"],
        admission_reason=values["admission_reason"],
        window_seconds=_required_int(values["window_seconds"], "window_seconds"),
        safety_margin_seconds=_required_int(
            values["safety_margin_seconds"],
            "safety_margin_seconds",
            minimum=0,
        ),
    )


def read_cutover_marker(redis: CutoverRedis) -> CutoverMarker | None:
    raw = redis.hgetall(CUTOVER_MARKER_KEY)
    if not raw:
        return None
    decoded = {str(key): str(value) for key, value in raw.items()}
    return parse_cutover_marker(decoded)


def trusted_writer_version(root: Path) -> int:
    """从受信任工作树元数据读取 writer 协议版本；缺失文件视为旧 v1。"""

    path = root / WRITER_PROTOCOL_RELATIVE
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 1
    except OSError as exc:
        raise CutoverError("writer protocol metadata is unreadable") from exc
    try:
        import json

        document = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise CutoverError("writer protocol metadata is corrupt") from exc
    if not isinstance(document, dict):
        raise CutoverError("writer protocol metadata is corrupt")
    expected = {
        "schema_version",
        "writer_version",
        "window_seconds",
        "safety_margin_seconds",
    }
    if set(document) != expected:
        raise CutoverError("writer protocol metadata is corrupt")
    try:
        schema = int(document["schema_version"])
        version = int(document["writer_version"])
        window = int(document["window_seconds"])
        margin = int(document["safety_margin_seconds"])
    except (TypeError, ValueError) as exc:
        raise CutoverError("writer protocol metadata is corrupt") from exc
    if schema != 1 or version < 1 or window != COST_WINDOW_SECONDS:
        raise CutoverError("writer protocol metadata is unsupported")
    if margin != CUTOVER_SAFETY_MARGIN_SECONDS:
        raise CutoverError("writer protocol metadata is unsupported")
    return version


def bootstrap_greenfield_cutover(
    *,
    redis: CutoverRedis,
    root: Path,
    target_writer_version: int = WRITER_PROTOCOL_VERSION,
) -> CutoverResult:
    """空 marker 的受控绿地激活：没有旧 writer 可排空，直接写入 active_v2。

    已是 active_v2 时幂等成功。进行中的切换不得覆盖。业务请求不得调用。
    """

    admission = AdmissionView(state="open", reason="ok", owned=False)
    version = trusted_writer_version(root)
    if version < target_writer_version:
        raise CutoverError("unsupported old writer binary")
    try:
        code, _, _, redis_time = cas_cutover(
            redis,
            action="bootstrap",
            release_binding="",
            target_writer_version=target_writer_version,
            minimum_writer_version=target_writer_version,
        )
    except CutoverError as exc:
        return _result(
            ok=False,
            marker=None,
            admission=admission,
            redis_time=0,
            error=str(exc),
        )
    if code < 0:
        return _result(
            ok=False,
            marker=read_cutover_marker(redis),
            admission=admission,
            redis_time=redis_time,
            error="cutover control plane is unavailable",
        )
    if code == 0:
        return _result(
            ok=False,
            marker=read_cutover_marker(redis),
            admission=admission,
            redis_time=redis_time,
            error="cutover state conflict",
        )
    marker = read_cutover_marker(redis)
    if marker is None or marker.state != "active_v2":
        return _result(
            ok=False,
            marker=marker,
            admission=admission,
            redis_time=redis_time,
            error="cutover did not activate",
        )
    return _result(
        ok=True,
        marker=marker,
        admission=admission,
        redis_time=redis_time,
    )


def check_supported_launch(
    *,
    root: Path,
    marker: CutoverMarker | None,
    environment: str,
) -> None:
    """支持的启动包装器在执行前检查受信任版本，不依赖旧二进制自觉拒绝。"""

    _ = environment
    version = trusted_writer_version(root)
    if marker is None:
        # 空 marker 不得由业务请求写成 active。v2 树可以启动，consume 仍失败关闭。
        # 缺协议文件视为 v1，由启动包装器拒绝，不依赖旧二进制自觉退出。
        if version < WRITER_PROTOCOL_VERSION:
            raise CutoverError("unsupported old writer binary")
        return
    if version < marker.minimum_writer_version:
        raise CutoverError("unsupported old writer binary")
    if marker.state == "active_v2" and version < marker.target_writer_version:
        raise CutoverError("unsupported old writer binary")


def _cas_outcome(raw: Any) -> tuple[int, str, str, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise CutoverError("cutover control plane is unavailable")
    try:
        code = int(raw[0])
        redis_time = int(raw[3]) if raw[3] not in {"", None} else 0
    except (TypeError, ValueError) as exc:
        raise CutoverError("cutover control plane is unavailable") from exc
    return code, str(raw[1] or ""), str(raw[2] or ""), redis_time


def cas_cutover(
    redis: CutoverRedis,
    *,
    action: str,
    release_binding: str,
    target_writer_version: int,
    minimum_writer_version: int,
    expect_generation: int | None = None,
    expect_state: str = "",
) -> tuple[int, str, str, int]:
    try:
        raw = redis.eval(
            CUTOVER_CAS_LUA,
            1,
            CUTOVER_MARKER_KEY,
            action,
            "" if expect_generation is None else str(expect_generation),
            expect_state,
            release_binding,
            str(target_writer_version),
            str(minimum_writer_version),
            CUTOVER_ADMISSION_REASON,
            str(COST_WINDOW_SECONDS),
            str(CUTOVER_SAFETY_MARGIN_SECONDS),
        )
    except CutoverError:
        raise
    except Exception as exc:
        raise CutoverError("cutover control plane is unavailable") from exc
    return _cas_outcome(raw)


def _result(
    *,
    ok: bool,
    marker: CutoverMarker | None,
    admission: AdmissionView,
    redis_time: int,
    opened: bool = False,
    error: str = "",
    state: CutoverState | Literal[""] | None = None,
    generation: int | None = None,
) -> CutoverResult:
    return CutoverResult(
        ok=ok,
        state=state if state is not None else (marker.state if marker else ""),
        generation=generation if generation is not None else (marker.generation if marker else 0),
        redis_time=redis_time,
        admission_state=admission.state,
        admission_reason=admission.reason,
        opened=opened,
        error=error,
    )


def _reprobe(
    first: ProbeResult,
    retry: Callable[[], ProbeResult],
) -> ProbeResult:
    if first.status in {"timeout", "error"}:
        return retry()
    if first.status == "present":
        return retry()
    return first


def run_writer_cutover(
    *,
    redis: CutoverRedis,
    executor: WriterCutoverExecutor,
    release_binding: str,
    root: Path,
    target_writer_version: int = WRITER_PROTOCOL_VERSION,
    rollback: bool = False,
) -> CutoverResult:
    """推进冻结→窗口等待→激活；每步可重入，失败保持关闭且不自动开闸。"""

    if not release_binding or len(release_binding) > 128:
        raise CutoverError("release binding is invalid")
    opened = False
    admission = AdmissionView(state="closed", reason=CUTOVER_ADMISSION_REASON, owned=False)
    redis_time = 0
    try:
        marker = read_cutover_marker(redis)
    except CutoverError as exc:
        return _result(
            ok=False,
            marker=None,
            admission=admission,
            redis_time=0,
            error=str(exc),
        )

    if rollback:
        if marker is None:
            return _result(
                ok=False,
                marker=None,
                admission=admission,
                redis_time=0,
                error="cutover marker is missing",
            )
        action = "rollback_prepare"
        target_writer_version = 1
        min_writer = max(marker.minimum_writer_version, WRITER_PROTOCOL_VERSION)
    else:
        action = "prepare"
        min_writer = target_writer_version

    try:
        code, _, _, redis_time = cas_cutover(
            redis,
            action=action,
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
        )
        if code < 0:
            raise CutoverError("cutover marker conflict")
        if code == 0:
            raise CutoverError("cutover state conflict")
        marker = read_cutover_marker(redis)
        if marker is None:
            raise CutoverError("cutover marker is missing")
        admission = executor.close_admission(
            reason=CUTOVER_ADMISSION_REASON,
            generation=marker.generation,
        )
        admission = executor.query_admission()
        if admission.state != "closed":
            raise CutoverError("send admission did not stay closed")
        if marker.state == "active_v2" and not rollback:
            if admission.owned:
                admission = executor.open_admission_if_owned(reason=CUTOVER_ADMISSION_REASON)
                opened = admission.state == "open"
            return _result(
                ok=True,
                marker=marker,
                admission=admission,
                redis_time=redis_time,
                opened=opened,
            )

        isolate = _reprobe(executor.isolate_writers(), executor.probe_writers)
        if not isolate.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"old writer probe {isolate.status}",
            )
        inflight = _reprobe(
            executor.probe_in_flight_accepts(),
            executor.probe_in_flight_accepts,
        )
        if not inflight.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"in-flight probe {inflight.status}",
            )
        isolate = executor.probe_writers()
        if not isolate.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"old writer probe {isolate.status}",
            )

        code, _, _, redis_time = cas_cutover(
            redis,
            action="fence",
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
        )
        if code == -5:
            raise CutoverError("redis time anomaly")
        if code <= 0:
            raise CutoverError("cutover fence rejected")
        code, _, _, redis_time = cas_cutover(
            redis,
            action="wait",
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
        )
        if code == -5:
            raise CutoverError("redis time anomaly")
        if code <= 0:
            raise CutoverError("cutover wait rejected")
        marker = read_cutover_marker(redis)
        if marker is None:
            raise CutoverError("cutover marker is missing")
        finish_action = "rollback_finish" if rollback else "activate"
        code, _, _, redis_time = cas_cutover(
            redis,
            action=finish_action,
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=target_writer_version,
        )
        if code == -3:
            admission = executor.query_admission()
            return _result(
                ok=True,
                marker=marker,
                admission=admission,
                redis_time=redis_time,
                error="waiting for not_before",
            )
        if code == -5:
            raise CutoverError("redis time anomaly")
        if code <= 0:
            raise CutoverError("cutover activate rejected")
        marker = read_cutover_marker(redis)
        if marker is None:
            raise CutoverError("cutover marker is missing")
        if rollback:
            if marker.state != "preparing" or marker.minimum_writer_version != 1:
                raise CutoverError("cutover rollback did not finish")
        elif marker.state != "active_v2":
            raise CutoverError("cutover did not activate")
        launch_version = executor.trusted_writer_version(root)
        if launch_version < marker.minimum_writer_version:
            raise CutoverError("unsupported old writer binary")
        admission = executor.query_admission()
        if admission.owned:
            admission = executor.open_admission_if_owned(reason=CUTOVER_ADMISSION_REASON)
            opened = admission.state == "open"
        return _result(
            ok=True,
            marker=marker,
            admission=admission,
            redis_time=redis_time,
            opened=opened,
        )
    except CutoverError as exc:
        try:
            admission = executor.query_admission()
        except CutoverError:
            admission = AdmissionView(
                state="closed",
                reason=CUTOVER_ADMISSION_REASON,
                owned=False,
            )
        try:
            marker = read_cutover_marker(redis)
        except CutoverError:
            marker = None
        return _result(
            ok=False,
            marker=marker,
            admission=admission,
            redis_time=redis_time,
            opened=False,
            error=str(exc),
        )
