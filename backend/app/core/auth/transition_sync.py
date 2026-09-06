"""账号锁定/IP 封禁审计的 API 进程补写器，不依赖后续登录流量。"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

from app.build_info import APP_VERSION
from app.core.auth.observability import (
    observe_transition_dead_letter,
    observe_transition_envelope_invalid,
    observe_transition_integrity_gauges,
    observe_transition_integrity_repair,
    observe_transition_integrity_scan,
    observe_transition_integrity_stats_complete,
    observe_transition_lease_expired,
    observe_transition_orphan,
    observe_transition_pending,
)
from app.core.auth.security_events import (
    AuthSecurityEventWriter,
    AuthTransitionDeadLetter,
    DeadLetterReason,
    SqlAuthSecurityEventRepository,
    transition_dead_letter_hmac,
)
from app.core.auth.service import (
    INTEGRITY_STATS_PAGE_SIZE,
    LoginGuard,
    RedisKeyValue,
    TransitionClaimResult,
    writer_lease_ms,
)
from app.settings import Settings, get_settings

INTEGRITY_SCAN_CALLS_PER_TICK = 8
INTEGRITY_HASH_PROCESS_BUDGET = 64
INTEGRITY_SCAN_TIME_BUDGET_S = 1.0
INTEGRITY_STATS_PAGES_PER_TICK = 4

LOGGER = logging.getLogger(__name__)


class TransitionAlerter(Protocol):
    async def emit_orphan(self, *, reason: str, field_class: str) -> None: ...


class LogTransitionAlerter:
    """默认只落结构化日志；生产可注入写 alert_log 的实现。"""

    async def emit_orphan(self, *, reason: str, field_class: str) -> None:
        LOGGER.critical(
            "auth transition orphaned",
            extra={"reason": reason, "field_class": field_class},
        )


class SqlTransitionAlerter:
    """以 sms_accept 写 alert_log，渠道仅 log-sink。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def emit_orphan(self, *, reason: str, field_class: str) -> None:
        from app.services.alert import AlertService
        from app.services.alert_repository import SqlAlertRepository

        service = AlertService(SqlAlertRepository(self.settings))
        await service.emit(
            alert_type="auth_transition",
            level="crit",
            title="认证审计信封丢失",
            detail={"reason": reason, "field_class": field_class},
            dedup_key=f"auth-transition-orphan:{reason}",
            dedup_hours=4,
        )


class AuthTransitionReconciler:
    """扫描 due 索引、领取同一 Writer Lease，并以 sms_auth 补写审计。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any | None = None,
        security_events: AuthSecurityEventWriter | None = None,
        alerter: TransitionAlerter | None = None,
        interval_s: float = 5,
        build_version: str | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("transition reconcile interval must be positive")
        self.settings = settings or get_settings()
        self.store = store
        self._owns_runtime = store is None
        self.security_events = security_events
        self.alerter = alerter
        self.interval_s = interval_s
        self.build_version = build_version or APP_VERSION
        self._task: asyncio.Task[None] | None = None
        self._reconcile_lock = asyncio.Lock()
        self._clock = monotonic
        self.scan_calls_per_tick = INTEGRITY_SCAN_CALLS_PER_TICK
        self.hash_process_budget = INTEGRITY_HASH_PROCESS_BUDGET
        self.scan_time_budget_s = INTEGRITY_SCAN_TIME_BUDGET_S
        self.stats_pages_per_tick = INTEGRITY_STATS_PAGES_PER_TICK
        self.stats_page_size = INTEGRITY_STATS_PAGE_SIZE
        self._hash_scan_cursor = "0"
        self._pending_scan_batch: deque[str] = deque()
        self._scan_cycle_started_monotonic: float | None = None
        self._last_completed_scan_monotonic: float | None = None
        self._scan_cycles_completed = 0
        self._storage_identity: str | None = None
        self._open_stats_offset = 0
        self._due_stats_offset = 0
        self._stats_pending_acc = 0
        self._stats_due_acc = 0
        self._stats_open_done = False
        self._stats_due_done = False

    def _guard(self) -> LoginGuard:
        store = self.store
        if store is None:
            store = RedisKeyValue.from_url(self.settings.redis_auth_url)
            self.store = store
        writer = self.security_events
        if writer is None:
            writer = SqlAuthSecurityEventRepository(self.settings)
            self.security_events = writer
        if self.alerter is None:
            self.alerter = (
                SqlTransitionAlerter(self.settings)
                if self._owns_runtime
                else LogTransitionAlerter()
            )
        return LoginGuard(
            store,
            security_events=writer,
            lease_ms=writer_lease_ms(self.settings),
            owner="reconciler",
        )

    async def reconcile(self) -> int:
        """补写到期 transition，并修复 Hash/Due 反向不一致。"""

        async with self._reconcile_lock:
            return await self._reconcile_once()

    async def _reconcile_once(self) -> int:
        guard = self._guard()
        count, oldest = await guard.due_stats()
        observe_transition_pending(count, float(oldest))
        due = await guard.scan_due_transitions()
        settled = 0
        for transition_id in due:
            try:
                await self._settle(guard, transition_id)
            except Exception:
                LOGGER.exception("auth transition reconcile failed")
                continue
            settled += 1
        repaired = await self._repair_integrity(guard)
        for transition_id in repaired:
            try:
                await self._settle(guard, transition_id)
            except Exception:
                LOGGER.exception("auth transition reconcile failed")
                continue
            settled += 1
        await self._refresh_integrity_stats(guard)
        return settled

    async def _settle(self, guard: LoginGuard, transition_id: str) -> None:
        claimed = await guard.claim_due_transition(transition_id)
        if claimed.outcome == "orphaned" or claimed.state == "orphaned":
            await self._handle_orphan(
                claimed.reason or "missing_hash",
                claimed.field_class or "missing_hash",
                transition_id,
            )
            return
        if not claimed.lease_id:
            return
        if claimed.previous == "writing" and claimed.action == "auth_account_locked":
            observe_transition_lease_expired("auth_account_locked")
        elif claimed.previous == "writing" and claimed.action == "auth_ip_banned":
            observe_transition_lease_expired("auth_ip_banned")
        if not self._envelope_complete(claimed):
            observe_transition_envelope_invalid(claimed.field_class or "schema")
            await self._handle_orphan(
                "incomplete_envelope",
                claimed.field_class or "schema",
                transition_id,
            )
            return
        await guard.persist_claimed_transition(
            transition_id=transition_id,
            lease_id=claimed.lease_id,
            audit_state=claimed.state,
        )

    @staticmethod
    def _envelope_complete(claimed: TransitionClaimResult) -> bool:
        if claimed.action == "auth_account_locked":
            result_ok = claimed.result_code == "ACCOUNT_LOCKED"
        elif claimed.action == "auth_ip_banned":
            result_ok = claimed.result_code == "RATE_LIMITED"
        else:
            return False
        try:
            count = int(claimed.count_text)
            remaining = int(claimed.remaining_ttl_seconds)
            created = int(claimed.created_at_ms)
        except ValueError:
            return False
        return bool(
            result_ok
            and claimed.provider_code
            and claimed.ip
            and count >= 1
            and remaining >= 1
            and created >= 0
        )

    async def _handle_orphan(
        self,
        reason: str,
        field_class: str,
        transition_id: str,
    ) -> None:
        observe_transition_orphan(
            reason
            if reason in {"missing_hash", "incomplete_envelope", "id_mismatch"}
            else "incomplete_envelope"
        )
        observe_transition_dead_letter(
            reason
            if reason in {"missing_hash", "incomplete_envelope", "id_mismatch"}
            else "incomplete_envelope"
        )
        writer = self.security_events
        if writer is not None and hasattr(writer, "record_dead_letter"):
            letter_reason: DeadLetterReason = "incomplete_envelope"
            if reason == "missing_hash":
                letter_reason = "missing_hash"
            elif reason == "id_mismatch":
                letter_reason = "id_mismatch"
            record = AuthTransitionDeadLetter(
                transition_hmac=transition_dead_letter_hmac(transition_id),
                reason=letter_reason,
                field_class=field_class or "schema",
                discovered_at=datetime.now(UTC),
                build_version=self.build_version,
            )
            try:
                await writer.record_dead_letter(record)
            except Exception:
                LOGGER.exception("auth transition dead letter persist failed")
        alerter = self.alerter or LogTransitionAlerter()
        try:
            await alerter.emit_orphan(reason=reason, field_class=field_class)
        except Exception:
            LOGGER.exception("auth transition orphan alert failed")

    def reset_integrity_scan(self) -> None:
        """存储实例或拓扑重置后，从游标 0 开始新的完整遍历。"""

        self._hash_scan_cursor = "0"
        self._pending_scan_batch.clear()
        self._scan_cycle_started_monotonic = None
        self._open_stats_offset = 0
        self._due_stats_offset = 0
        self._stats_pending_acc = 0
        self._stats_due_acc = 0
        self._stats_open_done = False
        self._stats_due_done = False

    @property
    def scan_in_progress(self) -> bool:
        return (
            self._scan_cycle_started_monotonic is not None
            or self._hash_scan_cursor != "0"
            or bool(self._pending_scan_batch)
        )

    async def _maybe_reset_storage(self, guard: LoginGuard) -> None:
        identity = await guard.storage_identity()
        if self._storage_identity is None:
            self._storage_identity = identity
            return
        if identity != self._storage_identity:
            self._storage_identity = identity
            self.reset_integrity_scan()

    def _observe_scan(self, processed: int) -> None:
        age = 0.0
        if self._last_completed_scan_monotonic is not None:
            age = max(0.0, self._clock() - self._last_completed_scan_monotonic)
        observe_transition_integrity_scan(
            cycles_completed=self._scan_cycles_completed,
            in_progress=self.scan_in_progress,
            processed=processed,
            age_seconds=age,
        )

    def _mark_scan_cycle_started(self) -> None:
        if self._scan_cycle_started_monotonic is None:
            self._scan_cycle_started_monotonic = self._clock()

    def _maybe_complete_scan_cycle(self) -> None:
        if self._hash_scan_cursor != "0" or self._pending_scan_batch:
            return
        if self._scan_cycle_started_monotonic is None:
            return
        self._last_completed_scan_monotonic = self._clock()
        self._scan_cycles_completed += 1
        self._scan_cycle_started_monotonic = None

    def _budget_exhausted(self, started: float, processed: int) -> bool:
        return processed >= self.hash_process_budget or (
            self._clock() - started
        ) >= self.scan_time_budget_s

    async def _drain_pending(
        self,
        guard: LoginGuard,
        seen: set[str],
        repaired: list[str],
        *,
        started: float,
        processed: int,
    ) -> int:
        drained = 0
        while self._pending_scan_batch:
            if self._budget_exhausted(started, processed + drained):
                break
            key = self._pending_scan_batch.popleft()
            drained += 1
            transition_id = key.rsplit(":", 1)[-1]
            if not transition_id or transition_id in seen:
                continue
            seen.add(transition_id)
            if await self._repair_one(guard, transition_id):
                repaired.append(transition_id)
        return drained

    async def _repair_integrity(self, guard: LoginGuard) -> list[str]:
        """从上次游标继续反向 Hash 扫描；单轮有界，不丢未处理批次。"""

        await self._maybe_reset_storage(guard)
        seen: set[str] = set()
        repaired: list[str] = []
        started = self._clock()
        processed = 0
        try:
            for transition_id in await guard.scan_open_transitions():
                if self._budget_exhausted(started, processed):
                    break
                if transition_id in seen:
                    continue
                seen.add(transition_id)
                processed += 1
                if await self._repair_one(guard, transition_id):
                    repaired.append(transition_id)
            had_pending = bool(self._pending_scan_batch)
            if had_pending or self._hash_scan_cursor != "0":
                self._mark_scan_cycle_started()
            processed += await self._drain_pending(
                guard, seen, repaired, started=started, processed=processed
            )
            if (
                had_pending
                and self._hash_scan_cursor == "0"
                and not self._pending_scan_batch
            ):
                self._maybe_complete_scan_cycle()
                return repaired
            scan_calls = 0
            while (
                scan_calls < self.scan_calls_per_tick
                and not self._budget_exhausted(started, processed)
            ):
                cursor = self._hash_scan_cursor
                try:
                    next_cursor, keys = await guard.scan_transition_hashes(cursor)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("auth transition hash scan failed")
                    break
                scan_calls += 1
                self._mark_scan_cycle_started()
                self._pending_scan_batch.extend(str(key) for key in keys)
                self._hash_scan_cursor = next_cursor
                processed += await self._drain_pending(
                    guard, seen, repaired, started=started, processed=processed
                )
                if next_cursor == "0":
                    self._maybe_complete_scan_cycle()
                    break
            self._maybe_complete_scan_cycle()
        finally:
            self._observe_scan(processed)
        return repaired

    async def _refresh_integrity_stats(self, guard: LoginGuard) -> None:
        """有界翻页累积 Open/Due 统计，完整周期后才发布精确值。"""

        pages = 0
        try:
            while pages < self.stats_pages_per_tick and not self._stats_open_done:
                page_len, matches = await guard.integrity_stats_page(
                    "open", self._open_stats_offset, self.stats_page_size
                )
                pages += 1
                self._open_stats_offset += page_len
                self._stats_pending_acc += matches
                if page_len < self.stats_page_size:
                    self._stats_open_done = True
                    break
            while pages < self.stats_pages_per_tick and not self._stats_due_done:
                page_len, matches = await guard.integrity_stats_page(
                    "due", self._due_stats_offset, self.stats_page_size
                )
                pages += 1
                self._due_stats_offset += page_len
                self._stats_due_acc += matches
                if page_len < self.stats_page_size:
                    self._stats_due_done = True
                    break
        except asyncio.CancelledError:
            observe_transition_integrity_stats_complete(False)
            raise
        except Exception:
            LOGGER.exception("auth transition integrity stats failed")
            observe_transition_integrity_stats_complete(False)
            return
        if self._stats_open_done and self._stats_due_done:
            observe_transition_integrity_gauges(
                pending_without_due=self._stats_pending_acc,
                due_without_payload=self._stats_due_acc,
            )
            observe_transition_integrity_stats_complete(True)
            self._open_stats_offset = 0
            self._due_stats_offset = 0
            self._stats_pending_acc = 0
            self._stats_due_acc = 0
            self._stats_open_done = False
            self._stats_due_done = False
            return
        observe_transition_integrity_stats_complete(False)

    async def _repair_one(self, guard: LoginGuard, transition_id: str) -> bool:
        try:
            direction, outcome, field_class = await guard.repair_transition_integrity(
                transition_id
            )
        except Exception:
            LOGGER.exception("auth transition integrity repair failed")
            return False
        observe_transition_integrity_repair(direction, outcome)
        if outcome == "orphaned":
            await self._handle_orphan(
                "missing_hash" if field_class == "missing_hash" else "incomplete_envelope",
                field_class or "missing_hash",
                transition_id,
            )
            return False
        return outcome == "repaired"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auth-transition-reconciler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                LOGGER.exception("auth transition reconcile loop failed")
            await asyncio.sleep(self.interval_s)


def create_auth_transition_reconciler(
    settings: Settings | None = None,
    *,
    store: Any | None = None,
    security_events: AuthSecurityEventWriter | None = None,
    alerter: TransitionAlerter | None = None,
    interval_s: float = 5,
) -> AuthTransitionReconciler:
    return AuthTransitionReconciler(
        settings,
        store=store,
        security_events=security_events,
        alerter=alerter,
        interval_s=interval_s,
    )


def require_writer_lease_budget(settings: Settings) -> None:
    """配置校验：Lease 必须严格大于数据库最坏写入预算。"""

    budget_ms = int(
        (
            float(settings.db_pool_timeout_seconds)
            + float(settings.db_connect_timeout_seconds)
            + int(settings.db_api_statement_timeout_ms) / 1000.0
        )
        * 1000
    )
    if writer_lease_ms(settings) <= budget_ms:
        raise ValueError("auth writer lease does not cover database timeout budget")
