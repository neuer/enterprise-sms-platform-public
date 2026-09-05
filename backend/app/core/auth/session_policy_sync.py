"""AD 会话策略的 API 侧对账：PostgreSQL 权威行同步到 auth Redis。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from time import time
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.observability import (
    observe_session_policy_publish_lag,
    observe_session_policy_reconcile,
    observe_session_policy_revisions,
)
from app.core.auth.session_policy import (
    AuthSessionPolicy,
    AuthSessionPolicyConflict,
    compare_authoritative_policy,
    load_auth_session_policy,
    publish_auth_session_policy,
)
from app.core.runtime_resources import database_engine, redis_client
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)


class _AuthRedis:
    """只暴露策略 CAS/load 需要的 eval，避免就绪检查导入 LoginGuard。"""

    def __init__(self, url: str) -> None:
        self.redis = redis_client(url)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        try:
            return await self.redis.eval(script, numkeys, *args)
        except RedisError as error:
            raise SessionStateUnavailable("auth session store unavailable") from error


POLICY_SELECT = """
SELECT revision, ad_session_max_age_minutes,
       EXTRACT(EPOCH FROM updated_at)::bigint AS updated_at_epoch,
       min_accepted_policy_revision
FROM auth_session_policy
WHERE id = 1
"""


def policy_from_mapping(row: Any) -> AuthSessionPolicy:
    return AuthSessionPolicy(
        int(row["revision"]),
        int(row["ad_session_max_age_minutes"]),
        int(row["updated_at_epoch"] or 0),
        int(row.get("min_accepted_policy_revision") or 1),
    )


async def load_postgres_session_policy(settings: Settings | None = None) -> AuthSessionPolicy:
    """读取受理库中的权威策略行；缺失即失败关闭。"""

    selected = settings or get_settings()
    engine = database_engine(selected.database_url, component="api")
    async with engine.connect() as connection:
        result = await connection.execute(text(POLICY_SELECT))
        row = result.mappings().first()
    if row is None:
        raise SessionStateUnavailable("AD session policy unavailable")
    policy = policy_from_mapping(row)
    observe_session_policy_revisions(postgres_revision=policy.revision)
    return policy


async def load_redis_session_policy(store: Any) -> AuthSessionPolicy | None:
    try:
        return await load_auth_session_policy(store)
    except SessionStateUnavailable:
        return None


class AuthSessionPolicyReconciler:
    """比较 PG revision 与 Redis；落后则 CAS 恢复，超前/冲突失败关闭。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any | None = None,
        postgres_loader: Callable[[], Awaitable[AuthSessionPolicy]] | None = None,
        interval_s: float = 60,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("session policy reconcile interval must be positive")
        self.settings = settings or get_settings()
        self.store = store
        self.postgres_loader = postgres_loader
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def _store(self) -> Any:
        if self.store is None:
            self.store = _AuthRedis(self.settings.redis_auth_url)
        return self.store

    async def _postgres(self) -> AuthSessionPolicy:
        if self.postgres_loader is not None:
            return await self.postgres_loader()
        return await load_postgres_session_policy(self.settings)

    async def reconcile(self) -> str:
        """返回 aligned/missing/behind 或抛出超前/冲突。"""

        postgres = await self._postgres()
        redis = await load_redis_session_policy(self._store())
        outcome = compare_authoritative_policy(postgres, redis)
        now = time()
        if outcome == "aligned" and redis is not None:
            observe_session_policy_publish_lag(0)
            if redis.updated_at_epoch:
                observe_session_policy_publish_lag(0)
            observe_session_policy_reconcile("aligned")
            return outcome
        if outcome in {"missing", "behind"}:
            try:
                await publish_auth_session_policy(self._store(), postgres)
            except AuthSessionPolicyConflict:
                observe_session_policy_reconcile("conflict")
                raise
            except SessionStateUnavailable:
                observe_session_policy_reconcile("unavailable")
                raise
            observe_session_policy_publish_lag(
                0 if postgres.updated_at_epoch <= 0 else max(0.0, now - postgres.updated_at_epoch)
            )
            observe_session_policy_reconcile(outcome)
            return outcome
        observe_session_policy_reconcile(outcome)
        raise SessionStateUnavailable(f"AD session policy {outcome}")

    async def ensure_ready(self) -> None:
        """启动/就绪门禁：缺失或落后时同步一次，超前或冲突保持 503。"""

        await self.reconcile()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auth-session-policy-reconciler")

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
                LOGGER.exception("auth session policy reconcile failed")
            await asyncio.sleep(self.interval_s)


def create_auth_session_policy_reconciler(
    settings: Settings | None = None,
) -> AuthSessionPolicyReconciler:
    return AuthSessionPolicyReconciler(settings)


class AlignedAuthSessionPolicyLoader:
    """只比较 PostgreSQL 与 Redis，对齐才返回快照；从不发布或修复策略。"""

    def __init__(
        self,
        store: Any,
        *,
        postgres_loader: Callable[[], Awaitable[AuthSessionPolicy]] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.postgres_loader = postgres_loader
        self.settings = settings

    async def load(self) -> AuthSessionPolicy:
        if self.postgres_loader is not None:
            postgres = await self.postgres_loader()
        else:
            postgres = await load_postgres_session_policy(self.settings)
        redis = await load_redis_session_policy(self.store)
        outcome = compare_authoritative_policy(postgres, redis)
        if outcome != "aligned" or redis is None:
            raise SessionStateUnavailable(f"AD session policy {outcome}")
        return redis
