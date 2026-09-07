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
    "active_v1",
    "aborted_closed",
)
CutoverState = Literal[
    "preparing",
    "old_writers_fenced",
    "waiting_window",
    "active_v2",
    "active_v1",
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
local action, expected_generation, expected_state = ARGV[1], ARGV[2], ARGV[3]
local binding, target, minimum = ARGV[4], ARGV[5], ARGV[6]
local reason, window, margin = ARGV[7], ARGV[8], ARGV[9]
local now = tonumber(redis.call('TIME')[1])
local function field(name)
  local raw = redis.call('HGET', marker, name)
  if raw == false then return '' end
  return tostring(raw)
end
local typ = redis.call('TYPE', marker)
if type(typ) == 'table' then typ = typ.ok end
if typ ~= 'none' and typ ~= 'hash' then return {-2,'','',tostring(now)} end
local exists = typ == 'hash'
local generation, state = field('generation'), field('state')
local function result(code) return {code,state,generation,tostring(now)} end
local function positive(raw)
  local n = tonumber(raw)
  return n ~= nil and n >= 1 and n == math.floor(n)
end
local active = state == 'active_v2' or state == 'active_v1'
local legacy_bootstrap = state == 'active_v2' and field('release_binding') == ''
  and field('target_writer_version') == '2' and field('minimum_writer_version') == '2'
  and field('fence_time') ~= '' and field('fence_time') == field('not_before')
if exists then
  for _, name in ipairs({'schema_version','generation','target_writer_version',
    'minimum_writer_version','fence_time','not_before','state','release_binding',
    'admission_reason','window_seconds','safety_margin_seconds'}) do
    if redis.call('HGET',marker,name) == false then return result(-2) end
  end
  if redis.call('HLEN',marker) ~= 11 or field('schema_version') ~= '1'
      or not positive(generation) or not positive(field('target_writer_version'))
      or not positive(field('minimum_writer_version'))
      or field('window_seconds') ~= window or field('safety_margin_seconds') ~= margin
      or field('admission_reason') ~= reason then return result(-2) end
  if not active and state ~= 'preparing' and state ~= 'old_writers_fenced'
      and state ~= 'waiting_window' and state ~= 'aborted_closed' then return result(-2) end
  if active or state == 'old_writers_fenced' or state == 'waiting_window' then
    local fence, after = tonumber(field('fence_time')), tonumber(field('not_before'))
    if fence == nil or after == nil or fence < 0
        or (not legacy_bootstrap and after < fence + tonumber(window) + tonumber(margin))
        then return result(-2) end
    if active and field('minimum_writer_version') ~= field('target_writer_version')
        then return result(-2) end
    if (state == 'active_v1') ~= (active and field('target_writer_version') == '1')
        then return result(-2) end
  end
end
if not positive(target) or not positive(minimum) or binding == ''
    or reason ~= 'writer_cutover' or window ~= '60' or margin ~= '5' then return result(-2) end
if exists and (expected_generation == '' or expected_state == '') then return result(0) end
if expected_generation ~= '' and generation ~= expected_generation then return result(0) end
if expected_state ~= '' and state ~= expected_state then return result(0) end
local begin = action == 'prepare' or action == 'rollback_prepare'
if begin and active and not legacy_bootstrap and field('target_writer_version') == target then
  if now < tonumber(field('not_before')) then return result(-5) end
  return result(2)
end
local new_operation = begin and (active or state == 'aborted_closed')
if exists and not new_operation and action ~= 'takeover_prepare'
    and field('release_binding') ~= binding then return result(-6) end
local function write(gen, next_state, fence, after, min)
  redis.call('HSET',marker,'schema_version','1','generation',gen,
    'target_writer_version',target,'minimum_writer_version',min,
    'fence_time',fence,'not_before',after,'state',next_state,
    'release_binding',binding,'admission_reason',reason,
    'window_seconds',window,'safety_margin_seconds',margin)
  generation, state = tostring(gen), next_state
  return result(1)
end
if action == 'takeover_prepare' then
  if not exists or state ~= 'preparing' then return result(0) end
  return write(tonumber(generation)+1,'preparing','','',minimum)
end
if begin then
  if not exists then
    if action == 'rollback_prepare' then return result(0) end
    return write(1,'preparing','','',minimum)
  end
  if new_operation then
    if tonumber(minimum) < tonumber(field('minimum_writer_version')) then return result(-2) end
    return write(tonumber(generation)+1,'preparing','','',minimum)
  end
  if field('target_writer_version') ~= target then return result(-6) end
  if state == 'preparing' or state == 'old_writers_fenced' or state == 'waiting_window'
      then return result(1) end
  return result(0)
end
if not exists or field('target_writer_version') ~= target then return result(0) end
if action == 'invalidate_fence' then
  if state ~= 'old_writers_fenced' and state ~= 'waiting_window' then return result(0) end
  return write(generation,'preparing','','',minimum)
end
if action == 'fence' or action == 'refence' then
  if action == 'fence' and (state == 'old_writers_fenced' or state == 'waiting_window')
      then return result(1) end
  if state ~= 'preparing' and not (action == 'refence'
      and (state == 'old_writers_fenced' or state == 'waiting_window')) then return result(0) end
  return write(generation,'old_writers_fenced',tostring(now),
    tostring(now+tonumber(window)+tonumber(margin)),minimum)
end
if action == 'wait' then
  if state == 'waiting_window' then return result(1) end
  if state ~= 'old_writers_fenced' then return result(0) end
  if now < tonumber(field('fence_time')) then return result(-5) end
  return write(generation,'waiting_window',field('fence_time'),field('not_before'),minimum)
end
if action == 'activate' or action == 'rollback_finish' then
  if state ~= 'waiting_window' then return result(0) end
  if now < tonumber(field('fence_time')) then return result(-5) end
  if now < tonumber(field('not_before')) then return result(-3) end
  local completed = 'active_v2'
  if target == '1' then completed = 'active_v1' end
  return write(generation,completed,field('fence_time'),field('not_before'),target)
end
if action == 'abort' then
  return write(generation,'aborted_closed',field('fence_time'),field('not_before'),minimum)
end
return result(-4)
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

    @property
    def requires_recovery(self) -> bool:
        """识别旧 bootstrap 表示，仅允许重新隔离排空，不能作为完成证据。"""
        return (self.state == "active_v2" and self.release_binding == ""
                and self.target_writer_version == self.minimum_writer_version == 2
                and self.fence_time is not None and self.fence_time == self.not_before)

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
    marker = CutoverMarker(
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

    if (marker.schema_version != CUTOVER_SCHEMA_VERSION
            or marker.window_seconds != COST_WINDOW_SECONDS
            or marker.safety_margin_seconds != CUTOVER_SAFETY_MARGIN_SECONDS
            or marker.admission_reason != CUTOVER_ADMISSION_REASON):
        raise CutoverError("cutover marker is inconsistent")
    if (marker.state in {"active_v1", "active_v2", "old_writers_fenced", "waiting_window"}
            and not marker.requires_recovery
            and (marker.fence_time is None or marker.not_before is None
                or marker.not_before < marker.fence_time + marker.window_seconds
                    + marker.safety_margin_seconds)):
        raise CutoverError("cutover marker window is inconsistent")
    if (marker.state in {"active_v1", "active_v2"}
            and (marker.minimum_writer_version != marker.target_writer_version
                or (marker.state == "active_v1") != (marker.target_writer_version == 1))):
        raise CutoverError("cutover marker protocol is inconsistent")
    return marker


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
    return parse_writer_protocol(payload)


def parse_writer_protocol(payload: str) -> int:
    """解析受信任 Git 对象或工作树中的固定协议元数据。"""

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
    """兼容旧调用名，仅只读确认；缺失 marker 必须走正式隔离与窗口切换。"""

    if trusted_writer_version(root) < target_writer_version:
        raise CutoverError("unsupported old writer binary")
    marker = read_cutover_marker(redis)
    valid = (marker is not None and marker.state == "active_v2" and not marker.requires_recovery
             and marker.target_writer_version == target_writer_version
             and trusted_writer_version(root) == target_writer_version)
    return _result(
        ok=valid, marker=marker,
        admission=AdmissionView("open" if valid else "closed", CUTOVER_ADMISSION_REASON, False),
        redis_time=0, error="" if valid else "controlled writer cutover required",
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
    target_writer_version: int | None = None,
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

    try:
        trusted_version = executor.trusted_writer_version(root)
        if target_writer_version is not None and target_writer_version != trusted_version:
            raise CutoverError("target writer protocol does not match trusted metadata")
        target_writer_version = trusted_version
    except CutoverError as exc:
        return _result(ok=False, marker=marker, admission=admission, redis_time=0, error=str(exc))
    action = "rollback_prepare" if rollback else "prepare"
    min_writer = max(target_writer_version, marker.minimum_writer_version if marker else 1)

    try:
        code, cas_state, cas_generation, redis_time = cas_cutover(
            redis,
            action=action,
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
            expect_generation=marker.generation if marker else None,
            expect_state=marker.state if marker else "",
        )
        if code < 0:
            raise CutoverError("cutover marker conflict")
        if code == 0:
            raise CutoverError("cutover state conflict")
        marker = read_cutover_marker(redis)
        if marker is None:
            raise CutoverError("cutover marker is missing")
        if marker.generation != int(cas_generation) or marker.state != cas_state:
            raise CutoverError("cutover state conflict")
        if code == 2:
            return _result(ok=True, marker=marker, admission=executor.query_admission(),
                           redis_time=redis_time)
        operation_generation = marker.generation

        def owned_marker(state: str) -> CutoverMarker:
            """每次 CAS 回读均绑定本次代际，禁止读到后继操作后继续推进。"""
            current = read_cutover_marker(redis)
            if (current is None or current.generation != operation_generation
                    or current.release_binding != release_binding or current.state != state
                    or current.target_writer_version != target_writer_version):
                raise CutoverError("cutover state conflict")
            return current

        marker = owned_marker(cas_state)
        admission = executor.close_admission(
            reason=CUTOVER_ADMISSION_REASON,
            generation=marker.generation,
        )
        admission = executor.query_admission()
        if admission.state != "closed":
            raise CutoverError("send admission did not stay closed")
        def invalidate_fence(probe: ProbeResult) -> None:
            """隔离证据失效先持久化，再执行任何可能失败的后续探测。"""
            nonlocal marker, redis_time
            if marker is None:
                raise CutoverError("cutover marker is missing")
            if probe.confirmed_absent or marker.state not in {
                "old_writers_fenced", "waiting_window",
            }:
                return
            changed, state, _, redis_time = cas_cutover(
                redis, action="invalidate_fence", release_binding=release_binding,
                target_writer_version=trusted_version, minimum_writer_version=min_writer,
                expect_generation=operation_generation, expect_state=marker.state,
            )
            if changed != 1:
                raise CutoverError("cutover fence invalidation rejected")
            marker = owned_marker(state)

        if marker.state in {"old_writers_fenced", "waiting_window"}:
            invalidate_fence(executor.probe_writers())
        isolate = executor.isolate_writers()
        invalidate_fence(isolate)
        isolate = _reprobe(isolate, executor.probe_writers)
        if not isolate.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"old writer probe {isolate.status}",
            )
        inflight = executor.probe_in_flight_accepts()
        invalidate_fence(inflight)
        inflight = _reprobe(inflight, executor.probe_in_flight_accepts)
        if not inflight.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"in-flight probe {inflight.status}",
            )
        isolate = executor.probe_writers()
        invalidate_fence(isolate)
        if not isolate.confirmed_absent:
            return _result(
                ok=False,
                marker=marker,
                admission=executor.query_admission(),
                redis_time=redis_time,
                error=f"old writer probe {isolate.status}",
            )

        code, cas_state, _, redis_time = cas_cutover(
            redis,
            action="fence",
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
            expect_generation=marker.generation, expect_state=marker.state,
        )
        if code == -5:
            raise CutoverError("redis time anomaly")
        if code <= 0:
            raise CutoverError("cutover fence rejected")
        marker = owned_marker(cas_state)
        code, cas_state, _, redis_time = cas_cutover(
            redis,
            action="wait",
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=min_writer,
            expect_generation=marker.generation, expect_state=marker.state,
        )
        if code == -5:
            raise CutoverError("redis time anomaly")
        if code <= 0:
            raise CutoverError("cutover wait rejected")
        marker = owned_marker(cas_state)
        finish_action = "rollback_finish" if rollback else "activate"
        code, cas_state, _, redis_time = cas_cutover(
            redis,
            action=finish_action,
            release_binding=release_binding,
            target_writer_version=target_writer_version,
            minimum_writer_version=target_writer_version,
            expect_generation=marker.generation, expect_state=marker.state,
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
        marker = owned_marker(cas_state)
        if target_writer_version == 1:
            if marker.state != "active_v1" or marker.minimum_writer_version != 1:
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
