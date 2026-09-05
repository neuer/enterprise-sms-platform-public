"""账号锁定/IP 封禁审计的 API 进程补写器，不依赖后续登录流量。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

from app.build_info import APP_VERSION
from app.core.auth.observability import (
    observe_transition_dead_letter,
    observe_transition_envelope_invalid,
    observe_transition_integrity_gauges,
    observe_transition_integrity_repair,
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
    LoginGuard,
    RedisKeyValue,
    TransitionClaimResult,
    writer_lease_ms,
)
from app.settings import Settings, get_settings

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
        pending_without_due, due_without_payload = await guard.integrity_stats()
        observe_transition_integrity_gauges(
            pending_without_due=pending_without_due,
            due_without_payload=due_without_payload,
        )
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

    async def _repair_integrity(self, guard: LoginGuard) -> list[str]:
        seen: set[str] = set()
        repaired: list[str] = []
        for transition_id in await guard.scan_open_transitions():
            if transition_id in seen:
                continue
            seen.add(transition_id)
            if await self._repair_one(guard, transition_id):
                repaired.append(transition_id)
        cursor = "0"
        for _ in range(8):
            cursor, keys = await guard.scan_transition_hashes(cursor)
            for key in keys:
                transition_id = key.rsplit(":", 1)[-1]
                if transition_id in seen:
                    continue
                seen.add(transition_id)
                if await self._repair_one(guard, transition_id):
                    repaired.append(transition_id)
            if cursor == "0":
                break
        return repaired

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
