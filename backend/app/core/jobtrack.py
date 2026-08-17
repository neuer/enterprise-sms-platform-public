"""后台任务追踪、失败/心跳巡检与 log-sink 告警。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache, wraps
from typing import Any, ParamSpec, Protocol, TypeVar, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.runtime_resources import database_engine
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)
PHONE_IN_TEXT = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+")
MAX_ERROR_LENGTH = 512

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class JobSpec:
    """任务名称与预期运行间隔。"""

    job_name: str
    expect_interval_s: int


@dataclass(frozen=True, slots=True)
class JobRunSnapshot:
    """心跳巡检所需的最近运行摘要。"""

    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str


class JobRunRepository(Protocol):
    """job_run 持久化接口。"""

    async def start(self, job_name: str, started_at: datetime) -> int: ...

    async def finish(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        duration_ms: int,
        items: int,
        status: str,
        error: str | None,
    ) -> None: ...

    async def latest(self, job_name: str) -> JobRunSnapshot | None: ...

    async def consecutive_failures(self, job_name: str, *, limit: int) -> int: ...


class JobHealthRepository(Protocol):
    """心跳巡检只依赖只读运行摘要，便于最小权限实现。"""

    async def latest(self, job_name: str) -> JobRunSnapshot | None: ...

    async def consecutive_failures(self, job_name: str, *, limit: int) -> int: ...


class AlertSink(Protocol):
    """心跳巡检告警最小接口。"""

    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None: ...


class JobInspector(Protocol):
    """后台任务健康巡检的最小接口。"""

    async def inspect_once(self, specs: Sequence[JobSpec] | None = None) -> None: ...


class JobMonitorLease(Protocol):
    """多 API 进程间的心跳巡检领导租约。"""

    async def try_acquire(self) -> bool: ...

    async def release(self) -> None: ...


def utc_now() -> datetime:
    """返回显式 UTC 时区时间。"""

    return datetime.now(UTC)


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("job tracking clock must return timezone-aware datetime")
    return moment


def sanitize_error(error: BaseException) -> str:
    """对错误摘要中的手机号与常见凭据赋值做强制脱敏。"""

    value = f"{type(error).__name__}: {error}"
    value = PHONE_IN_TEXT.sub(
        lambda match: f"{match.group(1)[:3]}****{match.group(1)[-4:]}",
        value,
    )
    value = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    return value[:MAX_ERROR_LENGTH]


class SqlJobRunRepository:
    """使用短生命周期 asyncpg 连接持久化任务追踪记录。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def start(self, job_name: str, started_at: datetime) -> int:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO job_run (job_name, started_at, status)
                        VALUES (:job_name, :started_at, 'running')
                        RETURNING id
                        """
                    ),
                    {"job_name": job_name, "started_at": started_at},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    async def finish(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        duration_ms: int,
        items: int,
        status: str,
        error: str | None,
    ) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE job_run
                        SET finished_at = :finished_at,
                            duration_ms = :duration_ms,
                            items = :items,
                            status = :status,
                            error = :error
                        WHERE id = :run_id
                        """
                    ),
                    {
                        "run_id": run_id,
                        "finished_at": finished_at,
                        "duration_ms": duration_ms,
                        "items": items,
                        "status": status,
                        "error": error,
                    },
                )
        finally:
            await engine.dispose()

    async def latest(self, job_name: str) -> JobRunSnapshot | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT job_name, started_at, finished_at, status
                        FROM job_run
                        WHERE job_name = :job_name
                        ORDER BY started_at DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"job_name": job_name},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                return JobRunSnapshot(
                    job_name=str(row["job_name"]),
                    started_at=cast(datetime, row["started_at"]),
                    finished_at=cast(datetime | None, row["finished_at"]),
                    status=str(row["status"]),
                )
        finally:
            await engine.dispose()

    async def consecutive_failures(self, job_name: str, *, limit: int) -> int:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT status
                        FROM job_run
                        WHERE job_name = :job_name
                        ORDER BY started_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    {"job_name": job_name, "limit": limit},
                )
                failures = consecutive_unfinished_count(result.scalars())
                return failures
        finally:
            await engine.dispose()


def consecutive_unfinished_count(statuses: Iterable[str]) -> int:
    """连续失败含启动后未结束的 running，用于捕捉杀循环。"""

    failures = 0
    for status in statuses:
        if status in {"failed", "running"}:
            failures += 1
            continue
        break
    return failures


class JobTracker:
    """确保任务开始、结束与异常均进入 job_run。"""

    def __init__(
        self,
        repository: JobRunRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.clock = clock

    @staticmethod
    def _items(result: Any) -> int:
        return result if isinstance(result, int) and not isinstance(result, bool) else 0

    @staticmethod
    def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
        return max(0, round((finished_at - started_at).total_seconds() * 1000))

    async def _finish_failure(
        self,
        run_id: int,
        started_at: datetime,
        error: BaseException,
    ) -> None:
        finished_at = _require_aware(self.clock())
        try:
            await self.repository.finish(
                run_id,
                finished_at=finished_at,
                duration_ms=self._duration_ms(started_at, finished_at),
                items=0,
                status="failed",
                error=sanitize_error(error),
            )
        except Exception as tracking_error:
            error.add_note(f"job tracking finish also failed: {type(tracking_error).__name__}")

    async def run_sync(
        self,
        job_name: str,
        function: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """在单个事件循环中追踪同步 Celery 任务。"""

        started_at = _require_aware(self.clock())
        run_id = await self.repository.start(job_name, started_at)
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            await self._finish_failure(run_id, started_at, error)
            raise
        finished_at = _require_aware(self.clock())
        await self.repository.finish(
            run_id,
            finished_at=finished_at,
            duration_ms=self._duration_ms(started_at, finished_at),
            items=self._items(result),
            status="success",
            error=None,
        )
        return result

    def run_sync_blocking(
        self,
        job_name: str,
        function: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """通过 worker 持久 loop 追踪同步 Celery 入口。"""

        started_at = _require_aware(self.clock())
        run_id = run_worker_async(self.repository.start(job_name, started_at))
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            run_worker_async(self._finish_failure(run_id, started_at, error))
            raise
        finished_at = _require_aware(self.clock())
        run_worker_async(
            self.repository.finish(
                run_id,
                finished_at=finished_at,
                duration_ms=self._duration_ms(started_at, finished_at),
                items=self._items(result),
                status="success",
                error=None,
            )
        )
        return result

    async def run_async(
        self,
        job_name: str,
        function: Callable[P, Awaitable[R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """追踪异步任务且保持原异常传播。"""

        started_at = _require_aware(self.clock())
        run_id = await self.repository.start(job_name, started_at)
        try:
            result = await function(*args, **kwargs)
        except Exception as error:
            await self._finish_failure(run_id, started_at, error)
            raise
        finished_at = _require_aware(self.clock())
        await self.repository.finish(
            run_id,
            finished_at=finished_at,
            duration_ms=self._duration_ms(started_at, finished_at),
            items=self._items(result),
            status="success",
            error=None,
        )
        return result


JOB_SPECS: dict[str, JobSpec] = {}


@lru_cache
def get_default_tracker() -> JobTracker:
    """创建进程级默认 SQL 追踪器。"""

    return JobTracker(SqlJobRunRepository())


def tracked_job(
    job_name: str,
    *,
    expect_interval_s: int,
    tracker: JobTracker | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """声明任务心跳并保证每次调用进入 job_run。"""

    if not job_name or expect_interval_s <= 0:
        raise ValueError("job_name and expect_interval_s must be positive")
    spec = JobSpec(job_name, expect_interval_s)
    existing = JOB_SPECS.get(job_name)
    if existing is not None and existing != spec:
        raise ValueError(f"conflicting job spec: {job_name}")
    JOB_SPECS[job_name] = spec

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            async_function = cast(Callable[P, Awaitable[Any]], function)

            @wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                selected = tracker or get_default_tracker()
                return await selected.run_async(job_name, async_function, *args, **kwargs)

            async_wrapper.job_spec = spec  # type: ignore[attr-defined]
            return cast(Callable[P, R], async_wrapper)

        @wraps(function)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            selected = tracker or get_default_tracker()
            return selected.run_sync_blocking(job_name, function, *args, **kwargs)

        sync_wrapper.job_spec = spec  # type: ignore[attr-defined]
        return sync_wrapper

    return decorator


class JobHealthMonitor:
    """在 API 进程中检查任务心跳和连续失败。"""

    def __init__(
        self,
        repository: JobHealthRepository,
        alert_sink: AlertSink,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.alert_sink = alert_sink
        self.clock = clock

    async def inspect_once(self, specs: Sequence[JobSpec] | None = None) -> None:
        """执行一次巡检；无记录也视为心跳缺失。"""

        now = _require_aware(self.clock())
        selected_specs = tuple(specs) if specs is not None else tuple(JOB_SPECS.values())
        for spec in selected_specs:
            latest = await self.repository.latest(spec.job_name)
            stalled = latest is None or now - latest.started_at > timedelta(
                seconds=spec.expect_interval_s * 2
            )
            if (
                latest is not None
                and latest.status == "running"
                and latest.finished_at is None
                and now - latest.started_at > timedelta(seconds=spec.expect_interval_s)
            ):
                stalled = True
            if stalled:
                # 同一缺失心跳事件保持去重；任务恢复后 started_at 会变化，
                # 再次停摆必须形成新的告警，不能被上一事件的四小时窗口吞掉。
                incident = (
                    "never" if latest is None else latest.started_at.astimezone(UTC).isoformat()
                )
                await self.alert_sink.emit(
                    alert_type="job_stalled",
                    level="warn",
                    title=f"后台任务心跳缺失：{spec.job_name}",
                    detail={
                        "job_name": spec.job_name,
                        "expect_interval_s": spec.expect_interval_s,
                        "recommendation": "检查 beat 与 worker 状态并核对最近 job_run。",
                    },
                    dedup_key=f"job_stalled:{spec.job_name}:{incident}",
                )
            failures = await self.repository.consecutive_failures(spec.job_name, limit=3)
            if failures >= 3:
                await self.alert_sink.emit(
                    alert_type="job_failed",
                    level="crit",
                    title=f"后台任务连续失败：{spec.job_name}",
                    detail={
                        "job_name": spec.job_name,
                        "consecutive_failures": failures,
                        "recommendation": "检查最近三次错误摘要并人工触发验证。",
                    },
                    dedup_key=f"job_failed:{spec.job_name}",
                )


class JobHeartbeatService:
    """API lifespan 内运行的轻量异步巡检循环。"""

    def __init__(
        self,
        monitor: JobInspector,
        *,
        scan_interval_s: float = 60,
        lease: JobMonitorLease | None = None,
    ) -> None:
        if scan_interval_s <= 0:
            raise ValueError("scan_interval_s must be positive")
        self.monitor = monitor
        self.scan_interval_s = scan_interval_s
        self.lease = lease
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="job-heartbeat-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.lease is not None:
            await self.lease.release()

    async def inspect_once(self) -> bool:
        """仅由持有领导租约的 API 进程执行一次巡检。"""

        if self.lease is not None and not await self.lease.try_acquire():
            return False
        await self.monitor.inspect_once()
        return True

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.scan_interval_s)
                try:
                    await self.inspect_once()
                except Exception:
                    LOGGER.exception("job heartbeat inspection failed")
        finally:
            if self.lease is not None:
                await self.lease.release()


class SqlJobMonitorLease:
    """以专用 PostgreSQL 会话持有巡检领导权，连接终止即自动释放。"""

    LOCK_NAME = "sms-platform:job-heartbeat-monitor"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._connection: AsyncConnection | None = None
        self._connection_context: AbstractAsyncContextManager[AsyncConnection] | None = None

    async def try_acquire(self) -> bool:
        if self._connection is not None:
            return True

        engine = database_engine(self.settings.database_url)
        connection_context = cast(
            AbstractAsyncContextManager[AsyncConnection],
            engine.connect(),
        )
        connection = await connection_context.__aenter__()
        try:
            result = await connection.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"),
                {"name": self.LOCK_NAME},
            )
            acquired = bool(result.scalar_one())
            await connection.commit()
        except BaseException as error:
            await connection_context.__aexit__(
                type(error),
                error,
                error.__traceback__,
            )
            await engine.dispose()
            raise

        if not acquired:
            await connection_context.__aexit__(None, None, None)
            await engine.dispose()
            return False

        self._engine = engine
        self._connection = connection
        self._connection_context = connection_context
        return True

    async def release(self) -> None:
        connection = self._connection
        connection_context = self._connection_context
        engine = self._engine
        self._connection = None
        self._connection_context = None
        self._engine = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))"),
                {"name": self.LOCK_NAME},
            )
            await connection.commit()
        finally:
            if connection_context is not None:
                await connection_context.__aexit__(None, None, None)
            else:
                await connection.close()
            if engine is not None:
                await engine.dispose()


def create_default_heartbeat_service() -> JobHeartbeatService:
    """为 API 进程创建 SQL 心跳巡检服务。"""

    repository = SqlJobRunRepository()
    return JobHeartbeatService(
        JobHealthMonitor(repository, SqlAlertService()),
        lease=SqlJobMonitorLease(),
    )
