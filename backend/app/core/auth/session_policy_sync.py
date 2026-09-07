"""AD 会话策略的 API 侧对账：控制面对齐 PostgreSQL/Redis，请求面只读进程快照。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic, time
from typing import Any, Literal

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

POLICY_SNAPSHOT_REFRESH_INTERVAL_S = 5.0
POLICY_SNAPSHOT_MAX_STALENESS_S = 15.0
POLICY_RECONCILE_TIMEOUT_S = 2.0
POLICY_RECONCILE_BACKOFF_MIN_S = 0.5
POLICY_RECONCILE_BACKOFF_MAX_S = 5.0
PolicySnapshotHealth = Literal["ready", "unavailable", "conflict"]


def validate_policy_snapshot_windows(
    *,
    refresh_interval_s: float,
    max_staleness_s: float,
    reconcile_timeout_s: float,
) -> None:
    """刷新周期必须小于最大陈旧窗口；对账超时保持有界。"""

    if not 0.1 <= refresh_interval_s <= 60:
        raise ValueError("session policy refresh interval must be between 0.1 and 60")
    if not 1 <= max_staleness_s <= 120:
        raise ValueError("session policy max staleness must be between 1 and 120")
    if not 0.1 <= reconcile_timeout_s <= 10:
        raise ValueError("session policy reconcile timeout must be between 0.1 and 10")
    if refresh_interval_s >= max_staleness_s:
        raise ValueError("session policy refresh interval must be less than max staleness")


def policy_snapshot_propagation_deadline_s(
    *,
    refresh_interval_s: float = POLICY_SNAPSHOT_REFRESH_INTERVAL_S,
    max_staleness_s: float = POLICY_SNAPSHOT_MAX_STALENESS_S,
    reconcile_timeout_s: float = POLICY_RECONCILE_TIMEOUT_S,
) -> float:
    """策略缩短在代码中的传播上界：刷新周期 + 对账超时 + 最大陈旧窗口。"""

    return refresh_interval_s + reconcile_timeout_s + max_staleness_s


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


@dataclass(frozen=True, slots=True)
class AuthSessionPolicySnapshot:
    """进程内不可变策略快照；请求面不得改写或拼接不同世代字段。"""

    policy: AuthSessionPolicy | None
    verified_at_monotonic: float
    health: PolicySnapshotHealth
    snapshot_generation: int


EMPTY_POLICY_SNAPSHOT = AuthSessionPolicySnapshot(None, 0.0, "unavailable", 0)


class AuthSessionPolicySnapshotProvider:
    """API 进程共享的只读快照；普通读取不会续期或回源 PostgreSQL。"""

    def __init__(
        self,
        *,
        max_staleness_s: float = POLICY_SNAPSHOT_MAX_STALENESS_S,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_staleness_s <= 0:
            raise ValueError("session policy max staleness must be positive")
        self.max_staleness_s = max_staleness_s
        self.monotonic_clock = monotonic_clock
        self._snapshot = EMPTY_POLICY_SNAPSHOT

    def current(self) -> AuthSessionPolicySnapshot:
        return self._snapshot

    async def load(self) -> AuthSessionPolicy:
        """请求面读取健康且未过期的快照；不触发对账或策略发布。"""

        snapshot = self._snapshot
        if snapshot.health == "conflict":
            raise SessionStateUnavailable("AD session policy conflict")
        if snapshot.health != "ready" or snapshot.policy is None:
            raise SessionStateUnavailable("AD session policy unavailable")
        age = self.monotonic_clock() - snapshot.verified_at_monotonic
        if age > self.max_staleness_s:
            raise SessionStateUnavailable("AD session policy stale")
        return snapshot.policy

    def publish(self, snapshot: AuthSessionPolicySnapshot) -> AuthSessionPolicySnapshot:
        """原子发布整份快照；旧 generation 或旧 revision 不能覆盖新结果。"""

        current = self._snapshot
        if snapshot.snapshot_generation < current.snapshot_generation:
            return current
        if (
            snapshot.health == "ready"
            and current.health == "ready"
            and current.policy is not None
            and snapshot.policy is not None
            and snapshot.policy.revision < current.policy.revision
        ):
            return current
        self._snapshot = snapshot
        return snapshot

    def mark_unavailable(self, *, health: PolicySnapshotHealth = "unavailable") -> None:
        current = self._snapshot
        self.publish(
            AuthSessionPolicySnapshot(
                policy=None,
                verified_at_monotonic=current.verified_at_monotonic,
                health=health,
                snapshot_generation=current.snapshot_generation + 1,
            )
        )


class AuthSessionPolicyReconciler:
    """控制面单飞对账：比较 PG 与 Redis，CAS 修复后发布不可变快照。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: Any | None = None,
        postgres_loader: Callable[[], Awaitable[AuthSessionPolicy]] | None = None,
        snapshot: AuthSessionPolicySnapshotProvider | None = None,
        interval_s: float | None = None,
        max_staleness_s: float | None = None,
        reconcile_timeout_s: float | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        selected: Settings | None
        if settings is not None:
            selected = settings
        elif store is None or postgres_loader is None:
            selected = get_settings()
        else:
            selected = None
        self.settings = selected
        self.store = store
        self.postgres_loader = postgres_loader

        def _window(explicit: float | None, attr: str, default: float) -> float:
            if explicit is not None:
                return explicit
            if self.settings is None:
                return default
            return float(getattr(self.settings, attr, default))

        refresh = _window(
            interval_s,
            "auth_session_policy_refresh_interval_s",
            POLICY_SNAPSHOT_REFRESH_INTERVAL_S,
        )
        staleness = _window(
            max_staleness_s,
            "auth_session_policy_max_staleness_s",
            POLICY_SNAPSHOT_MAX_STALENESS_S,
        )
        timeout = _window(
            reconcile_timeout_s,
            "auth_session_policy_reconcile_timeout_s",
            POLICY_RECONCILE_TIMEOUT_S,
        )
        validate_policy_snapshot_windows(
            refresh_interval_s=refresh,
            max_staleness_s=staleness,
            reconcile_timeout_s=timeout,
        )
        self.interval_s = refresh
        self.max_staleness_s = staleness
        self.reconcile_timeout_s = timeout
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper or asyncio.sleep
        self.snapshot = snapshot or AuthSessionPolicySnapshotProvider(
            max_staleness_s=staleness,
            monotonic_clock=monotonic_clock,
        )
        self._task: asyncio.Task[None] | None = None
        self._inflight: asyncio.Task[str] | None = None
        self._flight_gate = asyncio.Lock()
        self._accepting_probes = True
        self._lifecycle = 0
        self._stopping: asyncio.Task[None] | None = None

    def _store(self) -> Any:
        if self.store is None:
            if self.settings is None:
                raise SessionStateUnavailable("AD session policy unavailable")
            self.store = _AuthRedis(self.settings.redis_auth_url)
        return self.store

    async def _postgres(self) -> AuthSessionPolicy:
        if self.postgres_loader is not None:
            return await self.postgres_loader()
        return await load_postgres_session_policy(self.settings)

    def _require_lifecycle(self, lifecycle: int) -> None:
        if not self._accepting_probes or lifecycle != self._lifecycle:
            raise asyncio.CancelledError

    def _publish_ready(self, policy: AuthSessionPolicy, lifecycle: int) -> None:
        self._require_lifecycle(lifecycle)
        current = self.snapshot.current()
        self.snapshot.publish(
            AuthSessionPolicySnapshot(
                policy=policy,
                verified_at_monotonic=self.monotonic_clock(),
                health="ready",
                snapshot_generation=current.snapshot_generation + 1,
            )
        )

    async def _reconcile_body(self, lifecycle: int) -> str:
        postgres = await self._postgres()
        self._require_lifecycle(lifecycle)
        redis = await load_redis_session_policy(self._store())
        self._require_lifecycle(lifecycle)
        outcome = compare_authoritative_policy(postgres, redis)
        now = time()
        if outcome == "aligned" and redis is not None:
            observe_session_policy_publish_lag(0)
            if redis.updated_at_epoch:
                observe_session_policy_publish_lag(0)
            observe_session_policy_reconcile("aligned")
            self._publish_ready(
                AuthSessionPolicy(
                    postgres.revision,
                    postgres.ad_session_max_age_minutes,
                    postgres.updated_at_epoch,
                    postgres.min_accepted_policy_revision,
                ),
                lifecycle,
            )
            return outcome
        if outcome in {"missing", "behind"}:
            try:
                await publish_auth_session_policy(self._store(), postgres)
            except AuthSessionPolicyConflict:
                observe_session_policy_reconcile("conflict")
                self._require_lifecycle(lifecycle)
                self.snapshot.mark_unavailable(health="conflict")
                raise
            except SessionStateUnavailable:
                observe_session_policy_reconcile("unavailable")
                raise
            observe_session_policy_publish_lag(
                0 if postgres.updated_at_epoch <= 0 else max(0.0, now - postgres.updated_at_epoch)
            )
            observe_session_policy_reconcile(outcome)
            self._publish_ready(postgres, lifecycle)
            return outcome
        observe_session_policy_reconcile(outcome)
        self._require_lifecycle(lifecycle)
        if outcome == "conflict":
            self.snapshot.mark_unavailable(health="conflict")
        else:
            self.snapshot.mark_unavailable(health="unavailable")
        raise SessionStateUnavailable(f"AD session policy {outcome}")

    async def _reconcile_once(self, lifecycle: int) -> str:
        try:
            async with asyncio.timeout(self.reconcile_timeout_s):
                return await self._reconcile_body(lifecycle)
        except TimeoutError:
            raise SessionStateUnavailable("AD session policy reconcile timeout") from None

    async def reconcile(self) -> str:
        """单飞对账：并发调用共用一次权威读取，失败不续期 verified_at。"""

        async with self._flight_gate:
            if not self._accepting_probes:
                raise SessionStateUnavailable("AD session policy reconciler stopped")
            inflight = self._inflight
            if inflight is None or inflight.done():
                inflight = asyncio.create_task(self._reconcile_once(self._lifecycle))
                self._inflight = inflight
                inflight.add_done_callback(self._probe_done)
        return await asyncio.shield(inflight)

    def _probe_done(self, task: asyncio.Task[str]) -> None:
        """内部任务自己释放引用；无人等待时仍消费异常。"""
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled():
            task.exception()

    async def ensure_ready(self) -> None:
        """启动/就绪门禁：缺失或落后时同步一次，超前或冲突保持 503。"""

        await self.reconcile()
        snapshot = self.snapshot.current()
        if snapshot.health != "ready" or snapshot.policy is None:
            raise SessionStateUnavailable("AD session policy unavailable")

    def start(self) -> None:
        if self._stopping is not None and not self._stopping.done():
            raise SessionStateUnavailable("AD session policy reconciler stopping")
        if not self._accepting_probes:
            self._lifecycle += 1
            self._accepting_probes = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="auth-session-policy-reconciler")

    async def stop(self) -> None:
        async with self._flight_gate:
            if self._stopping is None or self._stopping.done():
                self._accepting_probes = False
                self._lifecycle += 1
                self.snapshot.mark_unavailable()
                self._stopping = asyncio.create_task(self._stop_owned())
        await asyncio.shield(self._stopping)

    async def _stop_owned(self) -> None:
        """停止任务拥有调度器和内部探测；不持创建锁等待任务退出。"""
        tasks = [task for task in (self._task, self._inflight) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._inflight = None

    async def _run(self) -> None:
        backoff = POLICY_RECONCILE_BACKOFF_MIN_S
        while True:
            try:
                await self.reconcile()
                backoff = POLICY_RECONCILE_BACKOFF_MIN_S
                await self.sleeper(self.interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("auth session policy reconcile failed")
                await self.sleeper(backoff)
                backoff = min(backoff * 2, POLICY_RECONCILE_BACKOFF_MAX_S)


@dataclass(frozen=True, slots=True)
class AuthSessionPolicyRuntime:
    """同一 API 进程内 Facade、就绪检查与对账任务共享的策略运行时。"""

    snapshot: AuthSessionPolicySnapshotProvider
    reconciler: AuthSessionPolicyReconciler
    store: Any


_RUNTIME: AuthSessionPolicyRuntime | None = None


def create_auth_session_policy_runtime(
    settings: Settings | None = None,
    *,
    store: Any | None = None,
    postgres_loader: Callable[[], Awaitable[AuthSessionPolicy]] | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> AuthSessionPolicyRuntime:
    """装配共享快照 Provider 与单飞对账者。"""

    selected = settings or get_settings()
    snapshot = AuthSessionPolicySnapshotProvider(
        max_staleness_s=float(selected.auth_session_policy_max_staleness_s),
        monotonic_clock=monotonic_clock,
    )
    reconciler = AuthSessionPolicyReconciler(
        selected,
        store=store,
        postgres_loader=postgres_loader,
        snapshot=snapshot,
        monotonic_clock=monotonic_clock,
        sleeper=sleeper,
    )
    return AuthSessionPolicyRuntime(snapshot, reconciler, store)


def bind_auth_session_policy_runtime(runtime: AuthSessionPolicyRuntime) -> AuthSessionPolicyRuntime:
    global _RUNTIME
    _RUNTIME = runtime
    return runtime


def reset_auth_session_policy_runtime() -> None:
    global _RUNTIME
    _RUNTIME = None


def get_auth_session_policy_runtime(
    settings: Settings | None = None,
) -> AuthSessionPolicyRuntime:
    """返回进程内共享运行时；首次调用时按 Settings 装配。"""

    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = create_auth_session_policy_runtime(settings)
    return _RUNTIME


def create_auth_session_policy_reconciler(
    settings: Settings | None = None,
) -> AuthSessionPolicyReconciler:
    return get_auth_session_policy_runtime(settings).reconciler


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
