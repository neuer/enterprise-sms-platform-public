"""进程级有界 PostgreSQL/Redis 资源、连接指标与统一关闭入口。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Literal, Protocol, cast

from redis.asyncio import Redis
from sqlalchemy import event, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.audit_context import (
    decode_audit_context_key,
    sign_audit_context,
    sign_system_audit_context,
)
from app.core.auth.principal_context import current_audit_principal
from app.core.correlation import current_correlation_id

DatabaseUrl = str | URL
DATABASE_COMPONENTS = ("api", "worker", "beat", "metrics", "background")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DatabasePoolBudget:
    pool_size: int
    max_overflow: int
    pool_timeout_seconds: float
    connect_timeout_seconds: float
    statement_timeout_ms: int

    def __post_init__(self) -> None:
        if not 1 <= self.pool_size <= 50:
            raise ValueError("database pool_size is outside the safe range")
        if not 0 <= self.max_overflow <= 20:
            raise ValueError("database max_overflow is outside the safe range")
        if not 0.1 <= self.pool_timeout_seconds <= 30:
            raise ValueError("database pool timeout is outside the safe range")
        if not 0.1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("database connect timeout is outside the safe range")
        if not 100 <= self.statement_timeout_ms <= 120_000:
            raise ValueError("database statement timeout is outside the safe range")


DEFAULT_BUDGETS = {
    "api": DatabasePoolBudget(8, 2, 3.0, 3.0, 15_000),
    "worker": DatabasePoolBudget(3, 1, 3.0, 3.0, 30_000),
    "beat": DatabasePoolBudget(2, 0, 3.0, 3.0, 10_000),
    "metrics": DatabasePoolBudget(2, 0, 2.0, 2.0, 2_000),
    "background": DatabasePoolBudget(2, 0, 3.0, 3.0, 30_000),
}


@dataclass(slots=True)
class _DatabaseMetrics:
    acquisitions: int = 0
    wait_seconds: float = 0.0
    timeouts: int = 0
    leaked_on_shutdown: int = 0


@dataclass(frozen=True, slots=True)
class DatabaseComponentSnapshot:
    component: str
    open: int
    checked_out: int
    budget: int
    acquisitions: int
    wait_seconds: float
    timeouts: int
    leaked_on_shutdown: int


@dataclass(frozen=True, slots=True)
class RuntimeResourceSnapshot:
    database_open: int = 0
    database_checked_out: int = 0
    redis_open: int = 0
    redis_in_use: int = 0
    database_components: tuple[DatabaseComponentSnapshot, ...] = ()


class _AsyncContext(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, *args: object) -> Any: ...


class _MeasuredConnectionContext:
    def __init__(self, delegate: _AsyncContext, component: str) -> None:
        self.delegate = delegate
        self.component = component

    async def __aenter__(self) -> Any:
        started = monotonic()
        try:
            value = await self.delegate.__aenter__()
        except (SqlAlchemyTimeoutError, TimeoutError):
            _record_acquisition(self.component, monotonic() - started, timed_out=True)
            raise
        _record_acquisition(self.component, monotonic() - started, timed_out=False)
        return value

    async def __aexit__(self, *args: object) -> Any:
        return await self.delegate.__aexit__(*args)


class ManagedAsyncEngine:
    """共享 AsyncEngine；repository 的历史 dispose 调用不再关闭进程池。"""

    def __init__(self, engine: AsyncEngine, component: str, budget: DatabasePoolBudget) -> None:
        self._engine = engine
        self.component = component
        self.budget = budget

    @property
    def pool(self) -> Any:
        return self._engine.pool

    def connect(self) -> _MeasuredConnectionContext:
        return _MeasuredConnectionContext(
            cast(_AsyncContext, self._engine.connect()),
            self.component,
        )

    def begin(self) -> _MeasuredConnectionContext:
        return _MeasuredConnectionContext(cast(_AsyncContext, self._engine.begin()), self.component)

    async def dispose(self, *_: object, **__: object) -> None:
        """兼容旧 repository；共享池只允许统一 shutdown 关闭。"""

    async def close(self) -> None:
        await self._engine.dispose()

    def discard_after_fork(self) -> None:
        """子进程丢弃继承的 pool 状态，不触碰父进程仍持有的连接。"""

        self._engine.sync_engine.dispose(close=False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


_DATABASE_ENGINES: dict[tuple[str, DatabaseUrl], ManagedAsyncEngine] = {}
_DATABASE_METRICS: dict[str, _DatabaseMetrics] = {}
_REDIS_CLIENTS: dict[str, Any] = {}
_RESOURCE_LOCK = Lock()
_BUDGETS = dict(DEFAULT_BUDGETS)
_RUNTIME_COMPONENT = (
    os.environ.get("SMS_COMPONENT", "api").strip().casefold()
    if os.environ.get("SMS_COMPONENT", "api").strip().casefold() in DATABASE_COMPONENTS
    else "api"
)


def configure_runtime_resources(settings: object, *, component: str | None = None) -> None:
    """从非敏感运行配置加载各组件预算；已有 pool 不允许静默换预算。"""

    global _RUNTIME_COMPONENT
    selected = (component or _RUNTIME_COMPONENT).strip().casefold()
    if selected not in DATABASE_COMPONENTS:
        raise ValueError("invalid runtime database component")
    settings_value = cast(Any, settings)
    budgets = {
        name: DatabasePoolBudget(
            int(getattr(settings_value, f"db_{name}_pool_size")),
            int(getattr(settings_value, f"db_{name}_max_overflow")),
            float(settings_value.db_pool_timeout_seconds),
            float(settings_value.db_connect_timeout_seconds),
            int(getattr(settings_value, f"db_{name}_statement_timeout_ms")),
        )
        for name in DATABASE_COMPONENTS
    }
    with _RESOURCE_LOCK:
        if _DATABASE_ENGINES and budgets != _BUDGETS:
            raise RuntimeError("database budgets cannot change while pools are open")
        _BUDGETS.update(budgets)
        _RUNTIME_COMPONENT = selected


def runtime_component() -> str:
    return _RUNTIME_COMPONENT


def _audit_context_key(name: str) -> bytes | None:
    """生产缺失立即失败；测试 owner 连接可不配置独立 key。"""

    from app.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    try:
        return decode_audit_context_key(settings.credential(name))
    except RuntimeError:
        if settings.is_production:
            raise
        return None


def audit_transaction_settings(
    *,
    database_user: str = "",
    txid: int = 0,
    signing_key: bytes | None = None,
) -> dict[str, str]:
    """把关联 ID 与稳定主体拆分为事务局部 PostgreSQL 设置。"""

    correlation_id = current_correlation_id()
    assert correlation_id is not None
    principal = current_audit_principal()
    values = {
        "correlation_id": str(correlation_id),
        "subject_kind": principal.subject_kind if principal is not None else "",
        "actor_name": principal.actor_name if principal is not None else "",
        "account_id": (
            str(principal.actor_account_id)
            if principal is not None and principal.actor_account_id is not None
            else ""
        ),
        "identity_id": (
            str(principal.actor_identity_id)
            if principal is not None and principal.actor_identity_id is not None
            else ""
        ),
        "app_id": (
            str(principal.actor_app_id)
            if principal is not None and principal.actor_app_id is not None
            else ""
        ),
    }
    values["signature"] = (
        sign_audit_context(
            signing_key,
            txid=txid,
            database_user=database_user,
            **values,
        )
        if signing_key is not None and values["subject_kind"]
        else ""
    )
    return values


def _set_audit_transaction_context(connection: Connection) -> None:
    identity = connection.execute(
        text("SELECT current_user AS database_user,txid_current() AS txid")
    ).mappings().one()
    values = audit_transaction_settings(
        database_user=str(identity["database_user"]),
        txid=int(identity["txid"]),
        signing_key=(
            _audit_context_key("audit_context_key")
            if current_audit_principal() is not None
            else None
        ),
    )
    connection.execute(
        text(
            """
            SELECT
              set_config('sms.correlation_id',:correlation_id,TRUE),
              set_config('sms.audit_subject_kind',:subject_kind,TRUE),
              set_config('sms.audit_actor_name',:actor_name,TRUE),
              set_config('sms.audit_account_id',:account_id,TRUE),
              set_config('sms.audit_identity_id',:identity_id,TRUE),
              set_config('sms.audit_app_id',:app_id,TRUE),
              set_config('sms.audit_producer_domain','',TRUE),
              set_config('sms.audit_action','',TRUE),
              set_config('sms.audit_context_signature',:signature,TRUE)
            """
        ),
        values,
    )


async def bind_connection_audit_subject(
    connection: Any,
    *,
    subject_kind: str,
    actor_name: str,
    account_id: int | None = None,
    identity_id: int | None = None,
    app_id: int | None = None,
) -> None:
    """在已开始事务中绑定由权威业务记录解析出的稳定审计主体。"""

    human = (
        subject_kind == "human"
        and account_id is not None
        and identity_id is not None
        and app_id is None
    )
    application = (
        subject_kind == "api_app"
        and account_id is None
        and identity_id is None
        and app_id is not None
    )
    if not actor_name or not (human or application):
        raise ValueError("invalid stable audit subject")
    identity = (
        await connection.execute(
            text("SELECT current_user AS database_user,txid_current() AS txid")
        )
    ).mappings().one()
    correlation_id = current_correlation_id()
    assert correlation_id is not None
    values = {
        "correlation_id": str(correlation_id),
        "subject_kind": subject_kind,
        "actor_name": actor_name,
        "account_id": str(account_id) if account_id is not None else "",
        "identity_id": str(identity_id) if identity_id is not None else "",
        "app_id": str(app_id) if app_id is not None else "",
    }
    signing_key = _audit_context_key("audit_context_key")
    signature = (
        sign_audit_context(
            signing_key,
            txid=int(identity["txid"]),
            database_user=str(identity["database_user"]),
            **values,
        )
        if signing_key is not None
        else ""
    )
    await connection.execute(
        text(
            """
            SELECT
              set_config('sms.correlation_id',:correlation_id,TRUE),
              set_config('sms.audit_subject_kind',:subject_kind,TRUE),
              set_config('sms.audit_actor_name',:actor_name,TRUE),
              set_config('sms.audit_account_id',:account_id,TRUE),
              set_config('sms.audit_identity_id',:identity_id,TRUE),
              set_config('sms.audit_app_id',:app_id,TRUE),
              set_config('sms.audit_producer_domain','',TRUE),
              set_config('sms.audit_action','',TRUE),
              set_config('sms.audit_context_signature',:signature,TRUE)
            """
        ),
        {
            **values,
            "signature": signature,
        },
    )


async def bind_connection_system_audit(
    connection: Any,
    *,
    actor_name: str,
    action: str,
    producer_domain: Literal["api", "realtime", "bulk"] | None = None,
) -> None:
    """为当前事务绑定由独立 system key 签名的自治审计生产者。"""

    if not actor_name or not action:
        raise ValueError("invalid system audit producer")
    identity = (
        await connection.execute(
            text("SELECT current_user AS database_user,txid_current() AS txid")
        )
    ).mappings().one()
    correlation_id = current_correlation_id()
    assert correlation_id is not None
    from app.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    selected_domain = producer_domain or settings.audit_producer_domain
    if selected_domain is None and not settings.is_production:
        # 单元测试与本地直接运行缺省采用 API 域；生产必须由 Compose 显式声明。
        selected_domain = "api"
    if selected_domain not in {"api", "realtime", "bulk"}:
        raise RuntimeError("audit producer domain is unavailable")
    signing_key = _audit_context_key(
        f"audit_system_{selected_domain}_context_key"
    )
    signature = (
        sign_system_audit_context(
            signing_key,
            txid=int(identity["txid"]),
            database_user=str(identity["database_user"]),
            correlation_id=str(correlation_id),
            producer_domain=selected_domain,
            actor_name=actor_name,
            action=action,
        )
        if signing_key is not None
        else ""
    )
    await connection.execute(
        text(
            """
            SELECT
              set_config('sms.correlation_id',:correlation_id,TRUE),
              set_config('sms.audit_subject_kind','system',TRUE),
              set_config('sms.audit_actor_name',:actor_name,TRUE),
              set_config('sms.audit_account_id','',TRUE),
              set_config('sms.audit_identity_id','',TRUE),
              set_config('sms.audit_app_id','',TRUE),
              set_config('sms.audit_producer_domain',:producer_domain,TRUE),
              set_config('sms.audit_action',:action,TRUE),
              set_config('sms.audit_context_signature',:signature,TRUE)
            """
        ),
        {
            "correlation_id": str(correlation_id),
            "producer_domain": selected_domain,
            "actor_name": actor_name,
            "action": action,
            "signature": signature,
        },
    )


def database_engine(
    database_url: DatabaseUrl,
    *,
    component: str | None = None,
) -> AsyncEngine:
    """按组件与 DSN 复用有界池；同一进程绝不按请求重复创建 engine。"""

    selected = (component or _RUNTIME_COMPONENT).strip().casefold()
    if selected not in DATABASE_COMPONENTS:
        raise ValueError("invalid database component")
    key = (selected, database_url)
    with _RESOURCE_LOCK:
        managed = _DATABASE_ENGINES.get(key)
        if managed is None:
            budget = _BUDGETS[selected]
            engine = create_async_engine(
                database_url,
                hide_parameters=True,
                pool_size=budget.pool_size,
                max_overflow=budget.max_overflow,
                pool_timeout=budget.pool_timeout_seconds,
                pool_pre_ping=True,
                pool_recycle=1800,
                connect_args={
                    "timeout": budget.connect_timeout_seconds,
                    "server_settings": {
                        "application_name": f"sms-{selected}",
                        "statement_timeout": str(budget.statement_timeout_ms),
                        "search_path": "pg_catalog,public",
                    },
                },
            )
            if isinstance(engine, AsyncEngine):
                event.listen(engine.sync_engine, "begin", _set_audit_transaction_context)
            managed = ManagedAsyncEngine(engine, selected, budget)
            _DATABASE_ENGINES[key] = managed
            _DATABASE_METRICS.setdefault(selected, _DatabaseMetrics())
        return cast(AsyncEngine, managed)


def redis_client(redis_url: str) -> Any:
    """按 URL 复用异步 Redis 客户端及其连接池。"""

    with _RESOURCE_LOCK:
        client = _REDIS_CLIENTS.get(redis_url)
        if client is None:
            client = Redis.from_url(redis_url, decode_responses=True)
            _REDIS_CLIENTS[redis_url] = client
        return client


def _record_acquisition(component: str, waited: float, *, timed_out: bool) -> None:
    with _RESOURCE_LOCK:
        metrics = _DATABASE_METRICS.setdefault(component, _DatabaseMetrics())
        metrics.acquisitions += 1
        metrics.wait_seconds += max(0.0, waited)
        metrics.timeouts += int(timed_out)


def _pool_counts(engine: ManagedAsyncEngine) -> tuple[int, int]:
    pool = engine.pool
    try:
        checked_out = max(0, int(pool.checkedout()))
        try:
            open_count = checked_out + max(0, int(pool.checkedin()))
        except (AttributeError, TypeError, ValueError):
            open_count = max(0, int(pool.size()) + int(pool.overflow()))
        return open_count, checked_out
    except (AttributeError, TypeError, ValueError):
        return 0, 0


def resource_snapshot() -> RuntimeResourceSnapshot:
    """按固定组件返回低基数连接、等待、超时与 shutdown 泄漏指标。"""

    components: list[DatabaseComponentSnapshot] = []
    with _RESOURCE_LOCK:
        engines = tuple(_DATABASE_ENGINES.values())
        metrics_snapshot = {
            name: _DatabaseMetrics(
                value.acquisitions,
                value.wait_seconds,
                value.timeouts,
                value.leaked_on_shutdown,
            )
            for name, value in _DATABASE_METRICS.items()
        }
        clients = tuple(_REDIS_CLIENTS.values())
    for component in DATABASE_COMPONENTS:
        matching = [engine for engine in engines if engine.component == component]
        counts = [_pool_counts(engine) for engine in matching]
        open_count = sum(item[0] for item in counts)
        checked_out = sum(item[1] for item in counts)
        metrics = metrics_snapshot.get(component, _DatabaseMetrics())
        if matching or metrics.acquisitions or metrics.timeouts or metrics.leaked_on_shutdown:
            components.append(
                DatabaseComponentSnapshot(
                    component,
                    open_count,
                    checked_out,
                    sum(
                        engine.budget.pool_size + engine.budget.max_overflow for engine in matching
                    ),
                    metrics.acquisitions,
                    metrics.wait_seconds,
                    metrics.timeouts,
                    metrics.leaked_on_shutdown,
                )
            )
    redis_open = 0
    redis_in_use = 0
    for client in clients:
        pool = getattr(client, "connection_pool", None)
        try:
            pool_state = vars(pool)
            in_use = len(pool_state["_in_use_connections"])
            available = len(pool_state["_available_connections"])
        except (AttributeError, KeyError, TypeError):
            continue
        redis_in_use += in_use
        redis_open += in_use + available
    return RuntimeResourceSnapshot(
        sum(item.open for item in components),
        sum(item.checked_out for item in components),
        redis_open,
        redis_in_use,
        tuple(components),
    )


def discard_inherited_runtime_resources() -> None:
    """prefork 子进程启动时清空父进程缓存，避免跨进程复用连接。"""

    with _RESOURCE_LOCK:
        engines = tuple(_DATABASE_ENGINES.values())
        _DATABASE_ENGINES.clear()
        _DATABASE_METRICS.clear()
        _REDIS_CLIENTS.clear()
    for engine in engines:
        engine.discard_after_fork()


async def close_runtime_resources() -> None:
    """统一关闭 DB 与 Redis；关闭前记录未归还连接作为泄漏证据。"""

    with _RESOURCE_LOCK:
        clients = tuple(_REDIS_CLIENTS.values())
        engines = tuple(_DATABASE_ENGINES.values())
        _REDIS_CLIENTS.clear()
        _DATABASE_ENGINES.clear()
        for engine in engines:
            metrics = _DATABASE_METRICS.setdefault(
                engine.component,
                _DatabaseMetrics(),
            )
            leaked = _pool_counts(engine)[1]
            metrics.leaked_on_shutdown += leaked
            if leaked:
                LOGGER.error(
                    "database_connections_checked_out_at_shutdown",
                    extra={
                        "component": engine.component,
                        "checked_out": leaked,
                    },
                )
    close_errors: list[BaseException] = []
    for client in clients:
        try:
            await client.aclose()
        except BaseException as error:
            close_errors.append(error)
    for engine in engines:
        try:
            await engine.close()
        except BaseException as error:
            close_errors.append(error)
    if close_errors:
        raise RuntimeError("one or more runtime resources failed to close") from close_errors[0]
