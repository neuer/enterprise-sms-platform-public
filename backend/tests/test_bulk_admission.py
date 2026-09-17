from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any

import pytest
from celery.exceptions import Retry

from app.core import bulk_admission
from app.core.jobtrack import JOB_SPECS, JobTracker, tracked_job
from app.core.worker_runtime import WorkerAsyncRuntime


@pytest.mark.parametrize("cancel", [False, True])
def test_busy_long_task_retries_before_tracking_and_send_slot_stays_available(
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    lock = Lock()
    entered = Event()
    releases: list[bool] = []
    finish_rows: list[dict[str, Any]] = []
    starts: list[str] = []
    retry_calls: list[dict[str, Any]] = []
    runtime = WorkerAsyncRuntime()
    stop: asyncio.Event | None = None
    running: asyncio.Task[Any] | None = None

    class Lease:
        def __init__(self) -> None:
            self.acquired = False

        async def try_acquire(self) -> bool:
            self.acquired = lock.acquire(blocking=False)
            return self.acquired

        async def release(self) -> None:
            if self.acquired:
                self.acquired = False
                releases.append(True)
                lock.release()

    class Repository:
        async def start(self, job_name: str, _started_at: object) -> int:
            starts.append(job_name)
            return len(starts)

        async def finish(self, _run_id: int, **kwargs: Any) -> None:
            finish_rows.append(kwargs)

    class Task:
        def retry(self, **kwargs: Any) -> Retry:
            retry_calls.append(kwargs)
            raise Retry("synthetic task retry")

    monkeypatch.setattr(bulk_admission, "SqlBulkTaskLease", Lease)
    monkeypatch.setattr(bulk_admission, "current_task", Task())
    monkeypatch.setattr(bulk_admission, "run_worker_async", runtime.run)
    monkeypatch.setattr("app.core.jobtrack.run_worker_async", runtime.run)

    async def workload() -> int:
        nonlocal stop, running
        stop = asyncio.Event()
        running = asyncio.current_task()
        entered.set()
        await stop.wait()
        return 7

    @bulk_admission.bulk_task_admission
    @tracked_job("admission_test", expect_interval_s=300, tracker=JobTracker(Repository()))  # type: ignore[arg-type]
    def long_task() -> int:
        return runtime.run(workload())

    async def send_work() -> int:
        return 1

    async def finish_long() -> None:
        assert stop is not None and running is not None
        if cancel:
            running.cancel()
        else:
            stop.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(long_task)
            assert entered.wait(2)
            with pytest.raises(Retry):
                long_task()
            assert retry_calls[0]["countdown"] == 30
            assert retry_calls[0]["max_retries"] == 60
            assert starts == ["admission_test"]
            assert finish_rows == []  # busy 没有伪造成功或进入业务追踪。
            assert runtime.run(send_work()) == 1
            runtime.run(finish_long())
            if cancel:
                from concurrent.futures import CancelledError

                with pytest.raises(CancelledError):
                    first.result(timeout=2)
            else:
                assert first.result(timeout=2) == 7
            assert releases == [True]

        @bulk_admission.bulk_task_admission
        def resumed() -> int:
            return runtime.run(send_work())

        assert resumed() == 1
        assert releases == [True, True]
        assert len(finish_rows) == 1
        assert finish_rows[0]["status"] == ("failed" if cancel else "success")
    finally:
        runtime.close()
        JOB_SPECS.pop("admission_test", None)


def test_soft_limit_keeps_admission_until_delayed_coroutine_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from app.core import worker_runtime

    runtime = WorkerAsyncRuntime()
    lock = Lock()
    entered, cleaning = Event(), Event()
    cleanup_done: asyncio.Event | None = None
    armed = False
    interrupted = False
    deferred_release: list[bool] = []

    class Lease:
        async def try_acquire(self) -> bool:
            return lock.acquire(blocking=False)

        async def release(self) -> None:
            deferred_release.append(True)
            lock.release()

    real_submit = asyncio.run_coroutine_threadsafe

    class InterruptedFuture:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def result(self) -> Any:
            assert entered.wait(2)
            raise SoftTimeLimitExceeded()

        def cancel(self) -> bool:
            return bool(self.inner.cancel())

    def submit(coroutine: Any, loop: Any) -> Any:
        nonlocal interrupted
        result = real_submit(coroutine, loop)
        if armed and not interrupted:
            interrupted = True
            return InterruptedFuture(result)
        return result

    async def workload() -> int:
        nonlocal cleanup_done
        cleanup_done = asyncio.Event()
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await cleanup_done.wait()
        return 1

    async def allow_cleanup() -> None:
        assert cleanup_done is not None
        cleanup_done.set()

    monkeypatch.setattr(bulk_admission, "SqlBulkTaskLease", Lease)
    monkeypatch.setattr(bulk_admission, "run_worker_async", runtime.run)
    monkeypatch.setattr(bulk_admission, "defer_worker_cleanup", runtime.defer_cleanup)
    monkeypatch.setattr(worker_runtime, "CANCELLATION_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(worker_runtime.asyncio, "run_coroutine_threadsafe", submit)

    @bulk_admission.bulk_task_admission
    def long_task() -> int:
        nonlocal armed
        armed = True
        return runtime.run(workload())

    try:
        with pytest.raises(worker_runtime.WorkerCancellationPending):
            long_task()
        assert cleaning.wait(2)
        assert lock.locked()
        assert not deferred_release
        pending_cleanup = next(iter(runtime._deferred))
        runtime.run(allow_cleanup())
        pending_cleanup.result(timeout=2)
        runtime.run(allow_cleanup())
        assert runtime._deferred == set()
        assert deferred_release == [True]
        assert not lock.locked()
    finally:
        runtime.close()


@pytest.mark.parametrize("delayed_acquire", [False, True])
def test_acquire_soft_limit_releases_only_after_acquisition_has_stopped(
    monkeypatch: pytest.MonkeyPatch, delayed_acquire: bool
) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from app.core import worker_runtime

    runtime = WorkerAsyncRuntime()
    lock = Lock()
    entered = Event()
    cleanup_done: asyncio.Event | None = None
    interrupted = False
    business_starts: list[bool] = []
    releases: list[bool] = []
    delay = delayed_acquire

    class Lease:
        def __init__(self) -> None:
            self.acquired = False

        async def try_acquire(self) -> bool:
            nonlocal cleanup_done
            self.acquired = lock.acquire(blocking=False)
            if self.acquired:
                entered.set()
                if delay:
                    cleanup_done = asyncio.Event()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await cleanup_done.wait()
            return self.acquired

        async def release(self) -> None:
            if self.acquired:
                self.acquired = False
                lock.release()
                releases.append(True)

    real_submit = asyncio.run_coroutine_threadsafe

    class InterruptedFuture:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def result(self) -> Any:
            assert entered.wait(2)
            if not delayed_acquire:
                # loop 已经完成拿锁并保存连接；主线程尚未取得返回值即被软超时打断。
                assert self.inner.result(timeout=2)
            raise SoftTimeLimitExceeded()

        def cancel(self) -> bool:
            return bool(self.inner.cancel())

    def submit(coroutine: Any, loop: Any) -> Any:
        nonlocal interrupted
        result = real_submit(coroutine, loop)
        if not interrupted:
            interrupted = True
            return InterruptedFuture(result)
        return result

    async def allow_cleanup() -> None:
        assert cleanup_done is not None
        cleanup_done.set()

    monkeypatch.setattr(bulk_admission, "SqlBulkTaskLease", Lease)
    monkeypatch.setattr(bulk_admission, "run_worker_async", runtime.run)
    monkeypatch.setattr(bulk_admission, "defer_worker_cleanup", runtime.defer_cleanup)
    monkeypatch.setattr(worker_runtime, "CANCELLATION_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(worker_runtime.asyncio, "run_coroutine_threadsafe", submit)

    @bulk_admission.bulk_task_admission
    def long_task() -> int:
        business_starts.append(True)
        return 1

    try:
        error = (
            worker_runtime.WorkerCancellationPending if delayed_acquire else SoftTimeLimitExceeded
        )
        with pytest.raises(error):
            long_task()
        assert business_starts == []
        if delayed_acquire:
            assert lock.locked()
            assert releases == []
            pending_cleanup = next(iter(runtime._deferred))
            runtime.run(allow_cleanup())
            pending_cleanup.result(timeout=2)
            runtime.run(allow_cleanup())
        assert releases == [True]
        assert not lock.locked()
        assert runtime._deferred == set()
        delay = False
        assert long_task() == 1
        assert business_starts == [True]
        assert releases == [True, True]
    finally:
        runtime.close()


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "soft_limit", "caught_timeout"])
def test_bulk_admission_waits_for_real_bounded_thread_after_coroutine_exit(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from concurrent.futures import CancelledError

    from celery.exceptions import SoftTimeLimitExceeded

    from app.core import bounded_executor, worker_runtime

    runtime = WorkerAsyncRuntime()
    executor = bounded_executor.BoundedExecutor(max_workers=1, max_pending=1)
    lock = Lock()
    entered, thread_finish = Event(), Event()
    thread_done = Event()
    releases: list[bool] = []
    running: asyncio.Task[Any] | None = None
    armed = False
    interrupted = False

    class Lease:
        def __init__(self) -> None:
            self.acquired = False

        async def try_acquire(self) -> bool:
            self.acquired = lock.acquire(blocking=False)
            return self.acquired

        async def release(self) -> None:
            if self.acquired:
                self.acquired = False
                lock.release()
                releases.append(True)

    class Task:
        def retry(self, **kwargs: Any) -> Retry:
            assert kwargs["countdown"] == 30
            raise Retry("synthetic task retry")

    real_submit = asyncio.run_coroutine_threadsafe

    class InterruptedFuture:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def result(self) -> Any:
            assert entered.wait(2)
            raise SoftTimeLimitExceeded()

        def cancel(self) -> bool:
            return bool(self.inner.cancel())

    def submit(coroutine: Any, loop: Any) -> Any:
        nonlocal interrupted
        result = real_submit(coroutine, loop)
        if armed and not interrupted and outcome == "soft_limit":
            interrupted = True
            return InterruptedFuture(result)
        return result

    def thread_work() -> int:
        entered.set()
        assert thread_finish.wait(5)
        thread_done.set()
        return 7

    async def workload() -> int:
        nonlocal running
        running = asyncio.current_task()
        try:
            return await bounded_executor.run_bounded(
                thread_work, timeout_s=0.05 if "timeout" in outcome else 5
            )
        except TimeoutError:
            if outcome == "caught_timeout":
                return 7
            raise

    async def cancel_work() -> None:
        assert running is not None
        running.cancel()

    async def short_send() -> int:
        return 1

    monkeypatch.setattr(bounded_executor, "_current_executor", lambda _pool: executor)
    monkeypatch.setattr(bulk_admission, "SqlBulkTaskLease", Lease)
    monkeypatch.setattr(bulk_admission, "run_worker_async", runtime.run)
    monkeypatch.setattr(bulk_admission, "defer_worker_cleanup", runtime.defer_cleanup)
    monkeypatch.setattr(bulk_admission, "current_task", Task())
    monkeypatch.setattr(worker_runtime.asyncio, "run_coroutine_threadsafe", submit)

    @bulk_admission.bulk_task_admission
    def long_task() -> int:
        nonlocal armed
        armed = True
        return runtime.run(workload())

    @bulk_admission.bulk_task_admission
    def next_task() -> int:
        return 1

    try:
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(long_task)
            assert entered.wait(2)
            if outcome == "cancel":
                runtime.run(cancel_work())
            if outcome == "caught_timeout":
                assert result.result(timeout=2) == 7
            else:
                expected = {
                    "cancel": CancelledError,
                    "timeout": TimeoutError,
                    "soft_limit": SoftTimeLimitExceeded,
                }[outcome]
                with pytest.raises(expected):
                    result.result(timeout=2)
            assert lock.locked()
            assert not thread_done.is_set()
            assert releases == []
            with pytest.raises(Retry):
                next_task()
            assert runtime.run(short_send()) == 1
            pending_cleanup = next(iter(runtime._deferred))
            thread_finish.set()
            pending_cleanup.result(timeout=2)
            assert runtime.run(short_send()) == 1
            assert runtime._deferred == set()
            assert thread_done.is_set()
            assert releases == [True]
            assert next_task() == 1
            assert releases == [True, True]
    finally:
        thread_finish.set()
        executor.close()
        runtime.close()
