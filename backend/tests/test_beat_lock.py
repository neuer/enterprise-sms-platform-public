from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType
from typing import Any

import pytest


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


def test_beat_schedule_database_uses_writable_tmp_path() -> None:
    module = load_beat_module()

    assert "--schedule" in module.BEAT_COMMAND
    schedule_index = module.BEAT_COMMAND.index("--schedule") + 1
    assert module.BEAT_COMMAND[schedule_index] == "/tmp/celerybeat-schedule"


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
            return FakeRedis()

    monkeypatch.setattr(module, "Redis", FakeClient)
    monkeypatch.setattr(module, "run_beat", lambda _client, **_kwargs: 0)

    assert module.main() == 0
    assert '"schedule":17' in module.os.environ["SMS_BEAT_SCHEDULE_JSON"]
