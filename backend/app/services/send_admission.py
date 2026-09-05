"""发送积压准入：按权威快照做 OPEN / DEGRADED / CLOSED，不探测 broker。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from time import monotonic
from typing import Literal, Protocol

LOGGER = logging.getLogger(__name__)

AdmissionState = Literal["open", "degraded", "closed"]
ADMISSION_STATES: tuple[AdmissionState, ...] = ("open", "degraded", "closed")


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


@dataclass(frozen=True, slots=True)
class SendAdmissionLimits:
    """硬编码阈值；不写入 sys_config，避免扩大敏感配置授权面。"""

    degraded_outbox_active: int = 200
    closed_outbox_active: int = 2000
    degraded_oldest_s: int = 300
    closed_oldest_s: int = 600
    degraded_outbox_dead: int = 10
    closed_outbox_dead: int = 50
    degraded_uncertain: int = 20
    degraded_callback_dead: int = 20
    closed_callback_dead: int = 200
    degraded_vendor_failures: int = 3
    degraded_max_recipients: int = 20
    snapshot_ttl_s: float = 5.0


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


class SendAdmissionFactsPort(Protocol):
    async def load(self) -> SendAdmissionFacts: ...

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

    if facts.outbox_active >= limits.degraded_outbox_active:
        return "degraded", "outbox_backlog"
    if age >= limits.degraded_oldest_s:
        return "degraded", "outbox_oldest"
    if facts.outbox_dead >= limits.degraded_outbox_dead:
        return "degraded", "outbox_dead"
    if facts.uncertain_overdue >= limits.degraded_uncertain:
        return "degraded", "uncertain_overdue"
    if facts.callback_dead >= limits.degraded_callback_dead:
        return "degraded", "callback_backlog"
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
        facts.outbox_active >= limits.degraded_outbox_active // 2
        or age >= limits.degraded_oldest_s // 2
        or facts.outbox_dead >= limits.degraded_outbox_dead // 2
        or facts.uncertain_overdue >= limits.degraded_uncertain // 2
        or facts.callback_dead >= limits.degraded_callback_dead // 2
        or facts.vendor_failures >= max(1, limits.degraded_vendor_failures - 1)
        or facts.realtime_paused
        or facts.bulk_paused
    )


def evaluate_capacity(
    facts: SendAdmissionFacts,
    *,
    limits: SendAdmissionLimits | None = None,
    previous_state: str | None = None,
) -> tuple[AdmissionState, str]:
    """计算全局容量带。恢复时滞回在半阈值，避免刚离开 CLOSED 就放行洪峰。"""

    selected = limits or SendAdmissionLimits()
    raw_state, reason = _raw_capacity(facts, selected)
    if previous_state == "closed":
        if raw_state == "closed" or _in_closed_hysteresis(facts, selected):
            return "closed", reason if raw_state == "closed" else "recovery_hold"
        if raw_state == "degraded" or _in_degraded_hysteresis(facts, selected):
            return "degraded", reason if raw_state == "degraded" else "recovery_hold"
        return "open", "ok"
    if previous_state == "degraded":
        if raw_state == "closed":
            return "closed", reason
        if raw_state == "degraded" or _in_degraded_hysteresis(facts, selected):
            return "degraded", reason if raw_state == "degraded" else "recovery_hold"
        return "open", "ok"
    return raw_state, reason


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
    """把 raw 容量与持久 previous/hold 收成可保存的 state/reason/hold。"""

    if raw_state == "closed":
        return "closed", raw_reason, None
    previous_state = previous if previous in ADMISSION_STATES else "closed"
    active_hold = hold_until if hold_until is not None and hold_until > db_now else None
    if previous_state == "closed" and previous_reason == "bootstrap":
        return raw_state, raw_reason, None
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


def _apply_lane_and_request(
    facts: SendAdmissionFacts,
    *,
    category: str,
    recipient_count: int,
    state: AdmissionState,
    reason: str,
    limits: SendAdmissionLimits,
) -> SendAdmissionDecision:
    """全局容量带确定后再叠加 lane 心跳/暂停与请求规模，禁止二次查库。"""

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
        if recipient_count > limits.degraded_max_recipients:
            return SendAdmissionDecision(state, "degraded_volume", False, 30)
    return SendAdmissionDecision(state, reason, True, 0)


def decide(
    facts: SendAdmissionFacts,
    *,
    category: str,
    recipient_count: int,
    previous_state: str | None = None,
    limits: SendAdmissionLimits | None = None,
) -> SendAdmissionDecision:
    """把容量带叠到类别/规模：degraded 与 recovery_hold 都只放行少量 verify/notice。"""

    selected = limits or SendAdmissionLimits()
    state, reason = evaluate_capacity(
        facts,
        limits=selected,
        previous_state=previous_state,
    )
    return _apply_lane_and_request(
        facts,
        category=category,
        recipient_count=recipient_count,
        state=state,
        reason=reason,
        limits=selected,
    )


def authorize_from_snapshot(
    snapshot: SendAdmissionSnapshot,
    *,
    category: str,
    recipient_count: int,
    limits: SendAdmissionLimits | None = None,
) -> SendAdmissionDecision:
    """只在权威快照上叠加请求级约束，禁止再次评估全局容量。"""

    selected = limits or SendAdmissionLimits()
    return _apply_lane_and_request(
        snapshot.facts,
        category=category,
        recipient_count=recipient_count,
        state=snapshot.state,
        reason=snapshot.reason,
        limits=selected,
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
    ) -> None:
        self.repository = repository
        self.limits = limits or SendAdmissionLimits()
        self.clock = clock
        self.load_timeout_s = load_timeout_s
        self._lock = asyncio.Lock()
        self._snapshot: SendAdmissionSnapshot | None = None
        self._version = 0

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
            state, reason = await self._persist_control_state(
                facts,
                load_control=load_control,
                save_control=save_control,
            )
            self._version += 1
            loaded = SendAdmissionSnapshot(
                self._version,
                now,
                facts,
                state,
                reason,
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
    ) -> tuple[AdmissionState, str, int, object]:
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
            if raw_valid_until is not None and _aware_datetime(raw_valid_until) < db_now:
                previous = "closed"
            else:
                loaded_state = persisted.get("state") or previous
                if loaded_state in ADMISSION_STATES:
                    previous = loaded_state
            if raw_hold_until is not None:
                hold_until = _aware_datetime(raw_hold_until)
            raw_epoch = persisted.get("state_epoch")
            if raw_epoch is None:
                epoch = 1
            elif isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool):
                epoch = raw_epoch
            else:
                raise SendAdmissionUnavailable()
            now_utc = db_now
        if persisted is None:
            previous = previous or "closed"
        state, reason = evaluate_capacity(
            facts,
            limits=self.limits,
            previous_state=previous,
        )
        state, reason, hold = transition_admission_state(
            previous=previous,
            raw_state=state,
            raw_reason=reason,
            db_now=now_utc,
            hold_until=hold_until,
            previous_reason=previous_reason,
        )
        return state, reason, epoch, hold

    @staticmethod
    def _can_adopt(
        candidate_state: str,
        candidate_reason: str,
        winner: dict[str, object],
    ) -> bool:
        from datetime import UTC, datetime

        raw_valid_until = winner.get("valid_until")
        raw_db_now = winner.get("db_now")
        if raw_valid_until is None:
            return False
        try:
            valid_until = _aware_datetime(raw_valid_until)
            db_now = (
                _aware_datetime(raw_db_now) if raw_db_now is not None else datetime.now(UTC)
            )
        except SendAdmissionUnavailable:
            return False
        if valid_until < db_now:
            return False
        winner_state = str(winner.get("state") or "")
        winner_reason = str(winner.get("reason_code") or winner.get("reason") or "")
        if winner_state == candidate_state and winner_reason == candidate_reason:
            return True
        return _state_rank(winner_state) >= _state_rank(candidate_state)

    async def _persist_control_state(
        self,
        facts: SendAdmissionFacts,
        *,
        load_control: Callable[..., Awaitable[object]] | None,
        save_control: Callable[..., Awaitable[object]] | None,
    ) -> tuple[AdmissionState, str]:
        if load_control is None and save_control is None:
            state, reason = evaluate_capacity(facts, limits=self.limits)
            return state, reason
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
            state, reason, epoch, hold = self._compute_from_persisted(
                facts,
                persisted if isinstance(persisted, dict) else None,
            )
            if save_control is None:
                return state, reason
            try:
                saved = await save_control(
                    state=state,
                    reason=reason,
                    hold_until=hold,
                    epoch=epoch,
                )
            except Exception as exc:
                last_error = exc
                if type(exc).__name__ == "AdmissionControlConflict":
                    winner = getattr(exc, "winner", None)
                    if isinstance(winner, dict) and self._can_adopt(state, reason, winner):
                        adopted = str(winner.get("state") or state)
                        adopted_reason = str(
                            winner.get("reason_code") or winner.get("reason") or reason
                        )
                        if adopted in ADMISSION_STATES:
                            return adopted, adopted_reason
                    continue
                self._snapshot = None
                raise SendAdmissionUnavailable() from exc
            if isinstance(saved, dict):
                outcome = str(saved.get("outcome") or "saved")
                saved_state = str(saved.get("state") or state)
                saved_reason = str(
                    saved.get("reason_code") or saved.get("reason") or reason
                )
                if outcome == "adopted" and not self._can_adopt(state, reason, saved):
                    continue
                if saved_state in ADMISSION_STATES:
                    return saved_state, saved_reason
            return state, reason
        self._snapshot = None
        raise SendAdmissionUnavailable() from last_error

    async def authorize(
        self,
        *,
        category: str,
        channel: str,
        recipient_count: int,
    ) -> None:
        """仅约束新发送。channel 只作无 PII 决策上下文，Web 与 API 同等积压。"""

        del channel
        snap = await self.snapshot()
        decision = authorize_from_snapshot(
            snap,
            category=category,
            recipient_count=max(1, int(recipient_count)),
            limits=self.limits,
        )
        if decision.allowed:
            return
        raise SendAdmissionRejected(
            decision.state,
            decision.reason,
            decision.retry_after_s,
        )


@lru_cache(maxsize=1)
def get_send_admission_guard() -> SendAdmissionGuard:
    """API 进程内单例，使短 TTL 快照在同实例请求间复用。"""

    from app.services.send_admission_repository import SqlSendAdmissionRepository

    return SendAdmissionGuard(SqlSendAdmissionRepository())
