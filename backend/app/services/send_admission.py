"""发送积压准入：按权威快照做 OPEN / DEGRADED / CLOSED，不探测 broker。"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from time import monotonic
from typing import Literal, NamedTuple, Protocol

LOGGER = logging.getLogger(__name__)

AdmissionState = Literal["open", "degraded", "closed"]
BudgetMode = Literal["normal", "degraded", "recovery", "closed"]
ADMISSION_STATES: tuple[AdmissionState, ...] = ("open", "degraded", "closed")


class CapacityEvaluation(NamedTuple):
    """容量原因与滞回事实独立于定时保持和请求预算。"""

    state: AdmissionState
    capacity_reason: str
    hysteresis_active: bool = False


class AdmissionControlState(NamedTuple):
    """一次权威状态转换的完整结果，CAS 胜者也必须重建此结果。"""

    state: AdmissionState
    reason: str
    state_epoch: int
    budget_mode: BudgetMode
    hold_until: datetime | None


def _budget_mode(
    state: AdmissionState,
    *,
    active_hold: bool,
    hysteresis_active: bool,
) -> BudgetMode:
    if state == "closed":
        return "closed"
    if state == "open":
        return "normal"
    return "recovery" if active_hold or hysteresis_active else "degraded"


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SendAdmissionUnavailable()
    return value


class SendAdmissionRejected(RuntimeError):
    """新发送被积压准入拒绝，对应 503 DEPENDENCY_UNAVAILABLE。"""

    def __init__(self, state: AdmissionState, reason: str, retry_after_s: int) -> None:
        self.state = state
        self.reason = reason
        self.retry_after_s = retry_after_s
        super().__init__("发送通道暂时不可用，请稍后重试")


class SendAdmissionUnavailable(SendAdmissionRejected):
    """准入快照过期或控制面不可用，必须失败关闭。"""

    def __init__(self) -> None:
        super().__init__("closed", "snapshot_unavailable", 5)


_STATE_RANK = {"open": 0, "degraded": 1, "closed": 2}
_CONTROL_CAS_ATTEMPTS = 4


def _state_rank(state: str) -> int:
    return _STATE_RANK.get(state, -1)


@dataclass(frozen=True, slots=True)
class SendAdmissionFacts:
    """发送准入所需的无 PII 积压事实；broker 健康只由 Outbox 是否排空推断。"""

    outbox_active: int
    outbox_oldest_age_s: int
    outbox_dead: int
    uncertain_overdue: int
    callback_dead: int
    realtime_paused: bool
    bulk_paused: bool
    vendor_failures: int
    realtime_heartbeat_stale: bool = False
    bulk_heartbeat_stale: bool = False
    dispatcher_heartbeat_stale: bool = False
    outbox_shared_active: int | None = None


@dataclass(frozen=True, slots=True)
class AppCallbackFacts:
    """应用回调事实涵盖尚未分发、合法未来重试和 dead，不读取投递正文。"""

    active: int
    dead: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (self.active, self.dead)):
            raise ValueError("callback admission counts must be non-negative integers")


@dataclass(frozen=True, slots=True)
class SendAdmissionLimits:
    """固定安全保护上限；应用回调限制低于全局硬容量，不扩大动态配置面。"""

    degraded_outbox_active: int = 200
    closed_outbox_active: int = 2000
    degraded_oldest_s: int = 300
    closed_oldest_s: int = 600
    degraded_outbox_dead: int = 10
    closed_outbox_dead: int = 50
    degraded_uncertain: int = 20
    app_callback_active: int = 200
    app_callback_dead: int = 20
    closed_callback_dead: int = 200
    degraded_vendor_failures: int = 3
    degraded_max_recipients: int = 20
    recovery_max_recipients: int = 20
    recovery_max_segments: int = 20
    recovery_max_chunks: int = 1
    recovery_accept_batches_per_second: int = 5
    recovery_accept_recipients_per_second: int = 20
    recovery_accept_segments_per_second: int = 20
    snapshot_ttl_s: float = 5.0

    def __post_init__(self) -> None:
        if not 0 < self.app_callback_active < self.closed_outbox_active:
            raise ValueError("application callback active cap must be below the global hard cap")
        if not 0 < self.app_callback_dead < self.closed_callback_dead:
            raise ValueError("application callback dead cap must be below the global hard cap")
        if self.degraded_max_recipients < 1 or self.recovery_max_recipients < 1:
            raise ValueError("recipient caps must be positive")
        if self.recovery_max_recipients > self.degraded_max_recipients:
            raise ValueError("recovery_max_recipients cannot exceed degraded_max_recipients")
        if (
            self.recovery_max_segments < 1
            or self.recovery_max_chunks < 1
            or self.recovery_accept_batches_per_second < 1
            or self.recovery_accept_recipients_per_second < 1
            or self.recovery_accept_segments_per_second < 1
        ):
            raise ValueError("recovery limits must be positive")


@dataclass(frozen=True, slots=True)
class SendAdmissionDecision:
    """容量带与单次请求是否放行。"""

    state: AdmissionState
    reason: str
    allowed: bool
    retry_after_s: int


@dataclass(frozen=True, slots=True)
class SendAdmissionSnapshot:
    """带版本与单调时钟的进程内准入快照。"""

    version: int
    loaded_at: float
    facts: SendAdmissionFacts
    state: AdmissionState
    reason: str
    budget_mode: BudgetMode
    state_epoch: int = 1
    hold_until: datetime | None = None


class SendAdmissionFactsPort(Protocol):
    async def load(self) -> SendAdmissionFacts: ...

    async def load_app_callbacks(self, app_id: int) -> AppCallbackFacts: ...

    async def record_transition(
        self,
        *,
        previous: str | None,
        state: str,
        reason: str,
        version: int,
        facts: SendAdmissionFacts,
    ) -> None: ...


def _age(facts: SendAdmissionFacts) -> int:
    return max(0, int(facts.outbox_oldest_age_s))


def _shared_active(facts: SendAdmissionFacts) -> int:
    """共享软保护排除回调，完整总量仍用于硬保护和指标。"""

    return facts.outbox_active if facts.outbox_shared_active is None else facts.outbox_shared_active


def _raw_capacity(
    facts: SendAdmissionFacts,
    limits: SendAdmissionLimits,
) -> tuple[AdmissionState, str]:
    """按当前事实落入 CLOSED / DEGRADED / OPEN，不含恢复滞回。"""

    age = _age(facts)
    if facts.dispatcher_heartbeat_stale:
        return "closed", "dispatcher_heartbeat_stale"
    if facts.realtime_heartbeat_stale and facts.bulk_heartbeat_stale:
        return "closed", "send_lanes_heartbeat_stale"
    if facts.realtime_paused and facts.bulk_paused:
        return "closed", "queues_paused"
    if facts.outbox_active >= limits.closed_outbox_active:
        return "closed", "outbox_backlog"
    if age >= limits.closed_oldest_s:
        return "closed", "outbox_oldest"
    if facts.outbox_dead >= limits.closed_outbox_dead:
        return "closed", "outbox_dead"
    if facts.callback_dead >= limits.closed_callback_dead:
        return "closed", "callback_backlog"

    if _shared_active(facts) >= limits.degraded_outbox_active:
        return "degraded", "outbox_backlog"
    if age >= limits.degraded_oldest_s:
        return "degraded", "outbox_oldest"
    if facts.outbox_dead >= limits.degraded_outbox_dead:
        return "degraded", "outbox_dead"
    if facts.uncertain_overdue >= limits.degraded_uncertain:
        return "degraded", "uncertain_overdue"
    if facts.vendor_failures >= limits.degraded_vendor_failures:
        return "degraded", "vendor_failures"
    if facts.realtime_paused or facts.bulk_paused:
        return "degraded", "queue_paused"
    return "open", "ok"


def _in_closed_hysteresis(
    facts: SendAdmissionFacts,
    limits: SendAdmissionLimits,
) -> bool:
    age = _age(facts)
    return (
        facts.outbox_active >= limits.closed_outbox_active // 2
        or age >= limits.closed_oldest_s // 2
        or facts.outbox_dead >= limits.closed_outbox_dead // 2
        or facts.callback_dead >= limits.closed_callback_dead // 2
        or (facts.realtime_paused and facts.bulk_paused)
    )


def _in_degraded_hysteresis(
    facts: SendAdmissionFacts,
    limits: SendAdmissionLimits,
) -> bool:
    age = _age(facts)
    return (
        _shared_active(facts) >= limits.degraded_outbox_active // 2
        or age >= limits.degraded_oldest_s // 2
        or facts.outbox_dead >= limits.degraded_outbox_dead // 2
        or facts.uncertain_overdue >= limits.degraded_uncertain // 2
        or facts.vendor_failures >= max(1, limits.degraded_vendor_failures - 1)
        or facts.realtime_paused
        or facts.bulk_paused
    )


def evaluate_capacity(
    facts: SendAdmissionFacts,
    *,
    limits: SendAdmissionLimits | None = None,
    previous_state: str | None = None,
) -> CapacityEvaluation:
    """计算全局容量带。恢复时滞回在半阈值，避免刚离开 CLOSED 就放行洪峰。"""

    selected = limits or SendAdmissionLimits()
    raw_state, reason = _raw_capacity(facts, selected)
    if previous_state == "closed":
        if raw_state == "closed" or _in_closed_hysteresis(facts, selected):
            return CapacityEvaluation(
                "closed",
                reason if raw_state == "closed" else "capacity_hysteresis",
                raw_state != "closed",
            )
        if raw_state == "degraded" or _in_degraded_hysteresis(facts, selected):
            return CapacityEvaluation(
                "degraded",
                reason if raw_state == "degraded" else "capacity_hysteresis",
                raw_state != "degraded",
            )
        return CapacityEvaluation("open", "ok")
    if previous_state == "degraded":
        if raw_state == "closed":
            return CapacityEvaluation("closed", reason)
        if raw_state == "degraded" or _in_degraded_hysteresis(facts, selected):
            return CapacityEvaluation(
                "degraded",
                reason if raw_state == "degraded" else "capacity_hysteresis",
                raw_state != "degraded",
            )
        return CapacityEvaluation("open", "ok")
    return CapacityEvaluation(raw_state, reason)


RECOVERY_HOLD_SECONDS = 60


def transition_admission_state(
    *,
    previous: str | None,
    raw_state: AdmissionState,
    raw_reason: str,
    db_now: datetime,
    hold_until: datetime | None,
    previous_reason: str | None = None,
) -> tuple[AdmissionState, str, datetime | None]:
    """把 raw 容量与持久 previous/hold 收成可保存的 state/reason/hold。

    previous_reason 只解释状态，不授权豁免；bootstrap 与普通 CLOSED 相同。
    """

    del previous_reason
    if raw_state == "closed":
        return "closed", raw_reason, None
    previous_state = previous if previous in ADMISSION_STATES else "closed"
    active_hold = hold_until if hold_until is not None and hold_until > db_now else None
    if previous_state == "closed":
        new_hold = db_now + timedelta(seconds=RECOVERY_HOLD_SECONDS)
        if active_hold is not None and active_hold > new_hold:
            new_hold = active_hold
        return "degraded", "recovery_hold", new_hold
    if active_hold is not None and raw_state == "open":
        return "degraded", "recovery_hold", active_hold
    if active_hold is not None:
        return raw_state, raw_reason, active_hold
    return raw_state, raw_reason, None


def _lane_for_category(category: str) -> Literal["realtime", "bulk"]:
    return "bulk" if category == "market" else "realtime"


def _positive_cost(value: int | None) -> int | None:
    if value is None:
        return None
    return max(1, int(value))


def _apply_lane_and_request(
    facts: SendAdmissionFacts,
    *,
    category: str,
    recipient_count: int,
    state: AdmissionState,
    reason: str,
    budget_mode: BudgetMode,
    limits: SendAdmissionLimits,
    estimated_segments: int | None = None,
    estimated_chunks: int | None = None,
) -> SendAdmissionDecision:
    """全局容量带确定后再叠加 lane 心跳/暂停与请求规模，禁止二次查库。"""

    valid_modes = {"open": {"normal"}, "degraded": {"degraded", "recovery"}, "closed": {"closed"}}
    if budget_mode not in valid_modes.get(state, set()):
        raise SendAdmissionUnavailable()
    if state == "closed":
        return SendAdmissionDecision(state, reason, False, 60)
    lane = _lane_for_category(category)
    if lane == "realtime" and facts.realtime_heartbeat_stale:
        return SendAdmissionDecision("closed", "realtime_heartbeat_stale", False, 60)
    if lane == "bulk" and facts.bulk_heartbeat_stale:
        return SendAdmissionDecision("closed", "bulk_heartbeat_stale", False, 60)
    if lane == "realtime" and facts.realtime_paused:
        return SendAdmissionDecision("closed", "realtime_paused", False, 60)
    if lane == "bulk" and facts.bulk_paused:
        return SendAdmissionDecision("closed", "bulk_paused", False, 60)
    if state == "degraded":
        if category == "market":
            return SendAdmissionDecision(state, "degraded_bulk", False, 30)
        recovering = budget_mode == "recovery"
        recipient_cap = (
            limits.recovery_max_recipients if recovering else limits.degraded_max_recipients
        )
        if recipient_count > recipient_cap:
            volume_reason = "recovery_volume" if recovering else "degraded_volume"
            return SendAdmissionDecision(state, volume_reason, False, 30)
        if recovering:
            segments = _positive_cost(estimated_segments)
            chunks = _positive_cost(estimated_chunks)
            if segments is not None and segments > limits.recovery_max_segments:
                return SendAdmissionDecision(state, "recovery_segment_cost", False, 30)
            if chunks is not None and chunks > limits.recovery_max_chunks:
                return SendAdmissionDecision(state, "recovery_segment_cost", False, 30)
    return SendAdmissionDecision(state, reason, True, 0)


def decide(
    facts: SendAdmissionFacts,
    *,
    category: str,
    recipient_count: int,
    previous_state: str | None = None,
    limits: SendAdmissionLimits | None = None,
    estimated_segments: int | None = None,
    estimated_chunks: int | None = None,
) -> SendAdmissionDecision:
    """把容量带叠到类别/规模：degraded 与 recovery_hold 都只放行少量 verify/notice。"""

    selected = limits or SendAdmissionLimits()
    capacity = evaluate_capacity(
        facts,
        limits=selected,
        previous_state=previous_state,
    )
    return _apply_lane_and_request(
        facts,
        category=category,
        recipient_count=recipient_count,
        state=capacity.state,
        reason=capacity.capacity_reason,
        budget_mode=_budget_mode(
            capacity.state, active_hold=False, hysteresis_active=capacity.hysteresis_active
        ),
        limits=selected,
        estimated_segments=estimated_segments,
        estimated_chunks=estimated_chunks,
    )


def authorize_from_snapshot(
    snapshot: SendAdmissionSnapshot,
    *,
    category: str,
    recipient_count: int,
    limits: SendAdmissionLimits | None = None,
    estimated_segments: int | None = None,
    estimated_chunks: int | None = None,
) -> SendAdmissionDecision:
    """只在权威快照上叠加请求级约束，禁止再次评估全局容量。"""

    selected = limits or SendAdmissionLimits()
    return _apply_lane_and_request(
        snapshot.facts,
        category=category,
        recipient_count=recipient_count,
        state=snapshot.state,
        reason=snapshot.reason,
        budget_mode=snapshot.budget_mode,
        limits=selected,
        estimated_segments=estimated_segments,
        estimated_chunks=estimated_chunks,
    )


class SendAdmissionGuard:
    """进程内短 TTL 快照；过期或加载失败一律失败关闭，不返回陈旧放行。"""

    def __init__(
        self,
        repository: SendAdmissionFactsPort,
        *,
        limits: SendAdmissionLimits | None = None,
        clock: Callable[[], float] = monotonic,
        load_timeout_s: float = 2.0,
        callback_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.repository = repository
        self.limits = limits or SendAdmissionLimits()
        self.clock = clock
        self.load_timeout_s = load_timeout_s
        self._lock = asyncio.Lock()
        self._snapshot: SendAdmissionSnapshot | None = None
        self._version = 0
        self._callback_clock = callback_clock
        self._callback_lock = asyncio.Lock()
        self._callback_cache: OrderedDict[int, tuple[float, AppCallbackFacts]] = OrderedDict()
        self._callback_cache_limit = 256

    def _fresh(self, now: float) -> SendAdmissionSnapshot | None:
        cached = self._snapshot
        if cached is None:
            return None
        if now - cached.loaded_at >= self.limits.snapshot_ttl_s:
            return None
        return cached

    async def snapshot(self) -> SendAdmissionSnapshot:
        now = float(self.clock())
        cached = self._fresh(now)
        if cached is not None:
            return cached
        async with self._lock:
            now = float(self.clock())
            cached = self._fresh(now)
            if cached is not None:
                return cached
            try:
                async with asyncio.timeout(self.load_timeout_s):
                    facts = await self.repository.load()
            except Exception as exc:
                self._snapshot = None
                raise SendAdmissionUnavailable() from exc
            load_control = getattr(self.repository, "load_control_state", None)
            save_control = getattr(self.repository, "save_control_state", None)
            control = await self._persist_control_state(
                facts,
                load_control=load_control,
                save_control=save_control,
            )
            self._version += 1
            loaded = SendAdmissionSnapshot(
                self._version,
                now,
                facts,
                control.state,
                control.reason,
                control.budget_mode,
                control.state_epoch,
                control.hold_until,
            )
            if (
                self._snapshot is None
                or self._snapshot.state != loaded.state
                or self._snapshot.reason != loaded.reason
            ):
                record = getattr(self.repository, "record_transition", None)
                if record is not None:
                    try:
                        await record(
                            previous=None if self._snapshot is None else self._snapshot.state,
                            state=loaded.state,
                            reason=loaded.reason,
                            version=loaded.version,
                            facts=facts,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "send admission transition alert unavailable",
                            extra={"error_type": type(exc).__name__},
                        )
            self._snapshot = loaded
            return loaded

    def _compute_from_persisted(
        self,
        facts: SendAdmissionFacts,
        persisted: dict[str, object] | None,
    ) -> AdmissionControlState:
        from datetime import UTC, datetime

        previous = None if self._snapshot is None else self._snapshot.state
        previous_reason: str | None = None
        epoch = 1
        hold_until: datetime | None = None
        now_utc = datetime.now(UTC)
        if persisted:
            raw_db_now = persisted.get("db_now")
            db_now = _aware_datetime(raw_db_now) if raw_db_now is not None else now_utc
            raw_db_reason = persisted.get("reason_code") or persisted.get("reason")
            if isinstance(raw_db_reason, str) and raw_db_reason:
                previous_reason = raw_db_reason
            raw_valid_until = persisted.get("valid_until")
            raw_hold_until = persisted.get("hold_until")
            loaded_state = persisted.get("state") or previous
            if raw_valid_until is not None and _aware_datetime(raw_valid_until) < db_now:
                # 已存在的保持或滞回继续用新 facts 评估，不因快照过期重开期限。
                # legacy recovery_hold + NULL 按滞回解释并在本次保存时规范化。
                if loaded_state == "degraded" and (
                    raw_hold_until is not None
                    or previous_reason in {"recovery_hold", "capacity_hysteresis"}
                ):
                    previous = "degraded"
                else:
                    previous = "closed"
            elif loaded_state in ADMISSION_STATES:
                previous = loaded_state
            if raw_hold_until is not None:
                hold_until = _aware_datetime(raw_hold_until)
            raw_epoch = persisted.get("state_epoch")
            if raw_epoch is None:
                epoch = 1
            elif isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 1:
                epoch = raw_epoch
            else:
                raise SendAdmissionUnavailable()
            now_utc = db_now
        if persisted is None:
            previous = previous or "closed"
        capacity = evaluate_capacity(
            facts,
            limits=self.limits,
            previous_state=previous,
        )
        state, reason, hold = transition_admission_state(
            previous=previous,
            raw_state=capacity.state,
            raw_reason=capacity.capacity_reason,
            db_now=now_utc,
            hold_until=hold_until,
            previous_reason=previous_reason,
        )
        return AdmissionControlState(
            state,
            reason,
            epoch,
            _budget_mode(
                state,
                active_hold=hold is not None and hold > now_utc,
                hysteresis_active=capacity.hysteresis_active,
            ),
            hold,
        )

    @staticmethod
    def _row_control(
        row: dict[str, object], fallback: AdmissionControlState
    ) -> AdmissionControlState:
        """用完整保存结果重建预算；兼容仅返回变更字段的仓储替身。"""
        from datetime import UTC

        raw_state = row.get("state", fallback.state)
        if raw_state not in ADMISSION_STATES:
            raise SendAdmissionUnavailable()
        state = raw_state
        reason = str(row.get("reason_code") or row.get("reason") or fallback.reason)
        epoch = row.get("state_epoch", fallback.state_epoch)
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise SendAdmissionUnavailable()
        raw_hold = row.get("hold_until", fallback.hold_until)
        hold = _aware_datetime(raw_hold) if raw_hold is not None else None
        now = _aware_datetime(row["db_now"]) if "db_now" in row else datetime.now(UTC)
        if state == "open" and hold is not None:
            raise SendAdmissionUnavailable()
        return AdmissionControlState(
            state,
            reason,
            epoch,
            _budget_mode(
                state,
                active_hold=hold is not None and hold > now,
                hysteresis_active=reason in {"capacity_hysteresis", "recovery_hold"},
            ),
            hold,
        )

    @staticmethod
    def _can_adopt(
        candidate: AdmissionControlState,
        winner: dict[str, object],
    ) -> bool:
        from datetime import UTC, datetime

        raw_valid_until = winner.get("valid_until")
        raw_db_now = winner.get("db_now")
        if raw_valid_until is None:
            return False
        try:
            valid_until = _aware_datetime(raw_valid_until)
            db_now = _aware_datetime(raw_db_now) if raw_db_now is not None else datetime.now(UTC)
        except SendAdmissionUnavailable:
            return False
        if valid_until < db_now:
            return False
        if not {"state", "state_epoch", "hold_until"}.issubset(winner):
            return False
        try:
            adopted = SendAdmissionGuard._row_control(winner, candidate)
        except SendAdmissionUnavailable:
            return False
        if _state_rank(adopted.state) < _state_rank(candidate.state):
            return False
        if adopted.state_epoch < candidate.state_epoch:
            return False
        return not (
            adopted.state == candidate.state == "degraded"
            and candidate.budget_mode == "recovery"
            and adopted.budget_mode != "recovery"
        )

    async def _persist_control_state(
        self,
        facts: SendAdmissionFacts,
        *,
        load_control: Callable[..., Awaitable[object]] | None,
        save_control: Callable[..., Awaitable[object]] | None,
    ) -> AdmissionControlState:
        if load_control is None and save_control is None:
            capacity = evaluate_capacity(facts, limits=self.limits)
            return AdmissionControlState(
                capacity.state,
                capacity.capacity_reason,
                1,
                _budget_mode(
                    capacity.state, active_hold=False, hysteresis_active=capacity.hysteresis_active
                ),
                None,
            )
        last_error: Exception | None = None
        for _ in range(_CONTROL_CAS_ATTEMPTS):
            try:
                persisted = await load_control() if load_control is not None else None
            except Exception as exc:
                self._snapshot = None
                raise SendAdmissionUnavailable() from exc
            if persisted is not None and not isinstance(persisted, dict):
                self._snapshot = None
                raise SendAdmissionUnavailable()
            candidate = self._compute_from_persisted(
                facts,
                persisted if isinstance(persisted, dict) else None,
            )
            if save_control is None:
                return candidate
            try:
                saved = await save_control(
                    state=candidate.state,
                    reason=candidate.reason,
                    hold_until=candidate.hold_until,
                    epoch=candidate.state_epoch,
                )
            except Exception as exc:
                last_error = exc
                if type(exc).__name__ == "AdmissionControlConflict":
                    winner = getattr(exc, "winner", None)
                    if isinstance(winner, dict) and self._can_adopt(candidate, winner):
                        return self._row_control(winner, candidate)
                    continue
                self._snapshot = None
                raise SendAdmissionUnavailable() from exc
            if isinstance(saved, dict):
                outcome = str(saved.get("outcome") or "saved")
                if outcome == "adopted" and not self._can_adopt(candidate, saved):
                    continue
                return self._row_control(saved, candidate)
            return candidate
        self._snapshot = None
        raise SendAdmissionUnavailable() from last_error

    async def _authorize_app_callbacks(self, app_id: int) -> None:
        """有界缓存应用事实；等待或查询超时不使用过期允许结果。"""

        def cached() -> AppCallbackFacts | None:
            item = self._callback_cache.get(app_id)
            if item is None:
                return None
            loaded_at, facts = item
            if self._callback_clock() - loaded_at >= min(self.limits.snapshot_ttl_s, 5.0):
                self._callback_cache.pop(app_id, None)
                return None
            self._callback_cache.move_to_end(app_id)
            return facts

        facts = cached()
        if facts is None:
            try:
                async with asyncio.timeout(self.load_timeout_s), self._callback_lock:
                    facts = cached()
                    if facts is None:
                        started = self._callback_clock()
                        facts = await self.repository.load_app_callbacks(app_id)
                        if not isinstance(facts, AppCallbackFacts):
                            raise ValueError("invalid callback admission facts")
                        self._callback_cache[app_id] = (started, facts)
                        self._callback_cache.move_to_end(app_id)
                        while len(self._callback_cache) > self._callback_cache_limit:
                            self._callback_cache.popitem(last=False)
            except Exception as exc:
                self._callback_cache.pop(app_id, None)
                raise SendAdmissionUnavailable() from exc
        if (
            facts.active >= self.limits.app_callback_active
            or facts.dead >= self.limits.app_callback_dead
        ):
            raise SendAdmissionRejected("closed", "app_callback_backlog", 5)

    async def authorize(
        self,
        *,
        app_id: int,
        category: str,
        channel: str,
        recipient_count: int,
        estimated_segments: int | None = None,
        estimated_chunks: int | None = None,
    ) -> None:
        """仅约束新发送；普通 Web 的服务端零应用上下文只受全局保护。"""

        if type(app_id) is not int or app_id < 0 or (app_id == 0 and channel != "web"):
            raise SendAdmissionUnavailable()
        snap = await self.snapshot()
        recipients = max(1, int(recipient_count))
        decision = authorize_from_snapshot(
            snap,
            category=category,
            recipient_count=recipients,
            estimated_segments=estimated_segments,
            estimated_chunks=estimated_chunks,
            limits=self.limits,
        )
        if not decision.allowed:
            raise SendAdmissionRejected(
                decision.state,
                decision.reason,
                decision.retry_after_s,
            )
        if app_id > 0:
            await self._authorize_app_callbacks(app_id)
        if snap.budget_mode != "recovery" or estimated_segments is None:
            return
        consume = getattr(self.repository, "consume_recovery_budget", None)
        if consume is None:
            raise SendAdmissionUnavailable()
        epoch = snap.state_epoch
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise SendAdmissionUnavailable()
        try:
            allowed = await consume(
                epoch=epoch,
                batches=1,
                recipients=recipients,
                segments=max(1, int(estimated_segments)),
                limits=self.limits,
            )
        except Exception as exc:
            raise SendAdmissionRejected("degraded", "recovery_rate", 1) from exc
        if not allowed:
            raise SendAdmissionRejected("degraded", "recovery_rate", 1)


@lru_cache(maxsize=1)
def get_send_admission_guard() -> SendAdmissionGuard:
    """API 进程内单例，使短 TTL 快照在同实例请求间复用。"""

    from app.services.send_admission_repository import SqlSendAdmissionRepository

    return SendAdmissionGuard(SqlSendAdmissionRepository())
