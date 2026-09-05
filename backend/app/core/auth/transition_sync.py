"""账号锁定/IP 封禁审计的 API 进程补写器，不依赖后续登录流量。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from app.core.auth.observability import (
    observe_transition_lease_expired,
    observe_transition_pending,
)
from app.core.auth.security_events import AuthSecurityEventWriter, SqlAuthSecurityEventRepository
from app.core.auth.service import LoginGuard, RedisKeyValue, writer_lease_ms
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)


class AuthTransitionReconciler:
    """扫描 due 索引、领取同一 Writer Lease，并以 sms_auth 补写审计。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any | None = None,
        security_events: AuthSecurityEventWriter | None = None,
        interval_s: float = 5,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("transition reconcile interval must be positive")
        self.settings = settings or get_settings()
        self.store = store
        self.security_events = security_events
        self.interval_s = interval_s
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
        return LoginGuard(
            store,
            security_events=writer,
            lease_ms=writer_lease_ms(self.settings),
            owner="reconciler",
        )

    async def reconcile(self) -> int:
        """补写到期 transition；返回本轮尝试次数。"""

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
        return settled

    async def _settle(self, guard: LoginGuard, transition_id: str) -> None:
        claimed = await guard.claim_due_transition(transition_id)
        lease_id, state, action = claimed[0], claimed[1], claimed[2]
        if not lease_id:
            return
        previous = claimed[8] if len(claimed) > 8 else ""
        if previous == "writing":
            observe_transition_lease_expired(action or "auth_account_locked")  # type: ignore[arg-type]
        provider = claimed[3] or "unknown"
        result_code = claimed[4] or (
            "ACCOUNT_LOCKED" if action == "auth_account_locked" else "RATE_LIMITED"
        )
        count = max(1, int(claimed[5] or 1))
        remaining = max(1, int(claimed[6] or 1))
        ip = claimed[7] or "0.0.0.0"
        await guard.persist_claimed_transition(
            action=action or "auth_account_locked",
            transition_id=transition_id,
            lease_id=lease_id,
            audit_state=state,
            provider_code=provider,
            result_code=result_code,
            count=count,
            remaining_ttl_seconds=remaining,
            ip=ip,
        )

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
    interval_s: float = 5,
) -> AuthTransitionReconciler:
    return AuthTransitionReconciler(
        settings,
        store=store,
        security_events=security_events,
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
