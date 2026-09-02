from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from celery import Celery
from celery.beat import PersistentScheduler


def load_beat_module() -> ModuleType:
    assert importlib.util.find_spec("app.tasks.beat") is not None, "beat 单实例锁尚未实现"
    return importlib.import_module("app.tasks.beat")


class FakeRedis:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.set_calls: list[tuple[Any, ...]] = []
        self.eval_calls: list[tuple[Any, ...]] = []
        self.closed = False

    def set(self, *args: Any, **kwargs: Any) -> bool:
        self.set_calls.append((*args, kwargs))
        return self.acquired

    def eval(self, *args: Any) -> int:
        self.eval_calls.append(args)
        return 1

    def close(self) -> None:
        self.closed = True


def test_lock_uses_nx_ttl_and_compare_scripts() -> None:
    module = load_beat_module()
    client = FakeRedis()
    lock = module.BeatLock(client, token="instance-a", ttl_s=30)

    assert lock.acquire() is True
    assert client.set_calls == [("lock:celery-beat", "instance-a", {"nx": True, "ex": 30})]
    assert lock.renew() is True
    assert lock.release() is True
    assert len(client.eval_calls) == 2
    assert client.eval_calls[0][-2:] == ("instance-a", 30)
    assert client.eval_calls[1][-1] == "instance-a"


def test_lock_contention_prevents_beat_process_start() -> None:
    module = load_beat_module()
    client = FakeRedis(acquired=False)
    process_started = False

    def process_factory(_command: list[str]) -> Any:
        nonlocal process_started
        process_started = True
        raise AssertionError("锁竞争失败时不得启动 beat")

    result = module.run_beat(client, process_factory=process_factory)

    assert result == 75
    assert process_started is False


def test_term_request_stops_child_before_releasing_beat_lock() -> None:
    module = load_beat_module()
    client = FakeRedis()
    stopped = False

    class FakeProcess:
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 10
            assert self.terminated is True
            return 0

    process = FakeProcess()

    def request_stop(_seconds: float) -> None:
        nonlocal stopped
        stopped = True

    result = module.run_beat(
        client,
        process_factory=lambda _command: process,
        wait=request_stop,
        stop_requested=lambda: stopped,
    )

    assert result == 0
    assert process.terminated is True
    assert len(client.eval_calls) == 1
    assert client.eval_calls[0][0] == module.RELEASE_SCRIPT
    assert client.eval_calls[0][-2] == "lock:celery-beat"


def test_lost_lock_terminates_child_and_waits_before_release() -> None:
    module = load_beat_module()

    class LosingRedis(FakeRedis):
        def eval(self, *args: Any) -> int:
            self.eval_calls.append(args)
            if args[0] == module.RENEW_SCRIPT:
                return 0
            return 1

    class FakeProcess:
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 10
            assert self.terminated is True
            return 1

    process = FakeProcess()
    client = LosingRedis()
    result = module.run_beat(
        client,
        process_factory=lambda _command: process,
        wait=lambda _seconds: None,
    )

    assert result == 1
    assert process.terminated is True
    assert client.eval_calls[-1][0] == module.RELEASE_SCRIPT


def test_beat_schedule_database_uses_persistent_volume_path() -> None:
    module = load_beat_module()

    assert "--schedule" in module.BEAT_COMMAND
    schedule_index = module.BEAT_COMMAND.index("--schedule") + 1
    assert module.BEAT_COMMAND[schedule_index] == "/var/lib/sms/beat/celerybeat-schedule"


def test_persistent_scheduler_restart_preserves_housekeeping_due_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """23 小时后的进程重启不得把每日任务重新顺延 24 小时。"""

    now = [datetime(2026, 9, 2, tzinfo=UTC)]
    schedule_path = tmp_path / "celerybeat-schedule"

    def scheduler() -> PersistentScheduler:
        app = Celery("beat-restart-test", broker="memory://")
        app.conf.update(
            timezone="Asia/Shanghai",
            enable_utc=True,
            result_expires=None,
            beat_schedule={
                "housekeeping": {
                    "task": "app.tasks.housekeeping",
                    "schedule": 86400,
                    "options": {"queue": "bulk"},
                }
            },
        )
        monkeypatch.setattr(app, "now", lambda: now[0])
        return PersistentScheduler(
            app=app,
            schedule_filename=str(schedule_path),
            lazy=False,
        )

    first = scheduler()
    try:
        original_last_run_at = first.schedule["housekeeping"].last_run_at
    finally:
        first.close()

    original_due_at = original_last_run_at + timedelta(days=1)
    now[0] += timedelta(hours=23)
    restarted = scheduler()
    try:
        entry = restarted.schedule["housekeeping"]
        assert entry.last_run_at == original_last_run_at

        before_due = entry.is_due()
        assert before_due.is_due is False
        assert before_due.next == pytest.approx(3600)
        assert now[0] + timedelta(seconds=before_due.next) == original_due_at

        now[0] = original_due_at
        assert entry.is_due().is_due is True
    finally:
        restarted.close()


def test_beat_main_loads_database_schedule_before_child_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_beat_module()
    monkeypatch.delenv("SMS_BEAT_SCHEDULE_JSON", raising=False)
    monkeypatch.setattr(
        module,
        "load_startup_schedule",
        lambda: {"poll-report": {"task": "app.tasks.poll_report", "schedule": 17}},
    )
    captured: dict[str, object] = {}

    class FakeClient:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> FakeRedis:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeRedis()

    monkeypatch.setattr(module, "Redis", FakeClient)
    monkeypatch.setattr(module, "run_beat", lambda _client, **_kwargs: 0)

    assert module.main() == 0
    assert '"schedule":17' in module.os.environ["SMS_BEAT_SCHEDULE_JSON"]
    assert captured["kwargs"]["socket_timeout"] == 2
    assert captured["kwargs"]["socket_connect_timeout"] == 2
