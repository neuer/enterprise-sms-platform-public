"""持有 Redis 租约后启动唯一 Celery beat 实例。"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol
from uuid import uuid4

from redis import Redis

from app.settings import get_settings
from app.tasks.scheduler import STARTUP_SCHEDULE_ENV, load_startup_schedule

LOGGER = logging.getLogger(__name__)
LOCK_KEY = "lock:celery-beat"
LOCK_TTL_S = 30
BEAT_COMMAND = [
    "celery",
    "-A",
    "app.tasks",
    "beat",
    "-l",
    "info",
    "--schedule",
    "/tmp/celerybeat-schedule",
]

RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisLockClient(Protocol):
    """beat 锁所需的最小 Redis 接口。"""

    def set(self, *args: Any, **kwargs: Any) -> Any: ...

    def eval(self, *args: Any) -> Any: ...


class BeatProcess(Protocol):
    """beat 子进程所需的最小控制接口。"""

    @property
    def returncode(self) -> int | None: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class BeatLock:
    """通过唯一 token 安全续租和释放 beat 单实例锁。"""

    def __init__(
        self,
        client: RedisLockClient,
        *,
        key: str = LOCK_KEY,
        token: str | None = None,
        ttl_s: int = LOCK_TTL_S,
    ) -> None:
        self.client = client
        self.key = key
        self.token = token or uuid4().hex
        self.ttl_s = ttl_s

    def acquire(self) -> bool:
        """以 NX 和有限租期竞争锁。"""

        return bool(self.client.set(self.key, self.token, nx=True, ex=self.ttl_s))

    def renew(self) -> bool:
        """仅锁持有者可以延长租期；Redis 异常视为失锁。"""

        try:
            return bool(self.client.eval(RENEW_SCRIPT, 1, self.key, self.token, self.ttl_s))
        except Exception:
            return False

    def release(self) -> bool:
        """仅锁持有者可以删除锁。"""

        return bool(self.client.eval(RELEASE_SCRIPT, 1, self.key, self.token))


def _start_process(command: list[str]) -> BeatProcess:
    return subprocess.Popen(command)  # noqa: S603


def run_beat(
    client: RedisLockClient,
    *,
    process_factory: Callable[[list[str]], BeatProcess] = _start_process,
    wait: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] = lambda: False,
) -> int:
    """竞争锁后运行 beat；锁丢失时立即终止调度进程。"""

    lock = BeatLock(client)
    if not lock.acquire():
        LOGGER.error("celery beat lock is already held")
        return 75

    process: BeatProcess | None = None
    try:
        process = process_factory(BEAT_COMMAND)
        while process.poll() is None:
            wait(LOCK_TTL_S / 3)
            if stop_requested():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    LOGGER.critical("celery beat child did not stop before lock release")
                    return 1
                return 0
            if lock.renew():
                continue
            LOGGER.critical("celery beat lock lease was lost; stopping scheduler")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                LOGGER.critical("celery beat child did not stop before lock release")
            return 1
        return process.returncode or 0
    finally:
        with suppress(Exception):
            lock.release()


def main() -> int:
    """从配置建立 Redis 连接并启动唯一 beat。"""

    os.environ[STARTUP_SCHEDULE_ENV] = json.dumps(
        load_startup_schedule(), sort_keys=True, separators=(",", ":")
    )
    settings = get_settings()
    client = Redis.from_url(
        settings.redis_control_url,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    stopped = threading.Event()
    managed_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in managed_signals}

    def request_stop(_signum: int, _frame: object | None) -> None:
        stopped.set()

    def wait_for_stop(seconds: float) -> None:
        stopped.wait(seconds)

    try:
        for signum in managed_signals:
            signal.signal(signum, request_stop)
        return run_beat(client, wait=wait_for_stop, stop_requested=stopped.is_set)
    finally:
        for signum in managed_signals:
            signal.signal(signum, previous_handlers[signum])
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
