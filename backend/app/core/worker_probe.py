"""不使用远程控制的职责队列消费探测；只交换短期随机挑战与固定运行元数据。"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from contextlib import suppress
from typing import Any

PROBE_TASK = "app.tasks.worker_probe"
PROBE_TIMEOUT_SECONDS = 10
PROBE_TTL_SECONDS = 30
PROBE_QUEUES = frozenset({"realtime", "realtime-report", "bulk", "callback"})


def expected_reply(queue: str, nonce: str, hostname: str) -> dict[str, object]:
    """仅允许明确队列和格式受限的挑战，不接受调用者自选任务或任意 Redis 键。"""

    if (
        queue not in PROBE_QUEUES
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or re.fullmatch(r"celery@[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", hostname) is None
    ):
        raise ValueError("invalid worker probe binding")
    return {
        "schema_version": 1, "nonce": nonce, "worker": hostname,
        "queue": queue, "exchange": queue, "routing_key": queue,
        "active_queues": [queue],
    }


def probe_key(backend: Any, nonce: str) -> Any:
    """沿用 Celery 按职责隔离的结果键前缀，禁止跨职责共享确认或响应。"""

    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid worker probe challenge")
    return backend.get_key_for_task("worker-probe-" + nonce)


def consume_probe(task: Any, nonce: str, role: str) -> None:
    """由真实任务进程响应；核验当前消费配置，不把容器存活等同于可消费。"""

    from app.core.broker_authorization import WORKER_QUEUES

    queue = WORKER_QUEUES.get(role)
    if queue is None:
        raise ValueError("worker probe requires a consumer capability")
    reply = expected_reply(queue, nonce, task.request.hostname)
    queues = task.app.amqp.queues.consume_from
    if set(queues) != {queue}:
        raise ValueError("worker probe queue binding failed")
    selected = queues[queue]
    if selected.routing_key != queue or selected.exchange.name != queue:
        raise ValueError("worker probe queue binding failed")
    if (task.request.delivery_info or {}).get("routing_key") != queue:
        raise ValueError("worker probe delivery binding failed")
    backend = task.app.backend
    backend.client.setex(
        probe_key(backend, nonce), PROBE_TTL_SECONDS, json.dumps(reply, sort_keys=True)
    )


def run_probe(app: Any, queue: str, nonce: str, hostname: str) -> dict[str, object]:
    """固定十秒期限；发布、消费或响应异常均失败，绝不回显原始异常或连接凭据。"""

    expected = expected_reply(queue, nonce, hostname)
    backend = app.backend
    key = probe_key(backend, nonce)
    client = backend.client
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    try:
        # 不使用 AsyncResult/get 或控制广播，避免结果订阅和额外队列能力。
        client.delete(key)
        app.send_task(
            PROBE_TASK, args=(nonce,), queue=queue, exchange=queue, routing_key=queue,
            expires=PROBE_TTL_SECONDS, ignore_result=True, retry=False, delivery_mode=1,
        )
        while time.monotonic() < deadline:
            raw = client.get(key)
            if raw is not None:
                if len(raw) > 4096 or json.loads(raw) != expected:
                    raise ValueError("worker probe response binding failed")
                return expected
            time.sleep(0.1)
        raise TimeoutError("worker probe timed out")
    finally:
        with suppress(Exception):
            client.delete(key)


def main() -> int:
    """仅在指定 worker 容器内使用其既有凭据，输出经绑定校验的最小 JSON。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, choices=sorted(PROBE_QUEUES))
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--hostname", required=True)
    args = parser.parse_args()
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from app.core.broker_authorization import WORKER_QUEUES
        from app.settings import get_settings
        from app.tasks import celery_app

        settings = get_settings()
        if settings.sms_component != "worker" or WORKER_QUEUES.get(
            settings.redis_broker_role
        ) != args.queue:
            raise ValueError("worker probe capability mismatch")
        # 只限制探测进程的连接等待；不修改运行中 worker 的配置或任何 ACL。
        celery_app.conf.update(
            broker_connection_timeout=3,
            redis_socket_connect_timeout=3,
            redis_socket_timeout=3,
            result_backend_always_retry=False,
        )
        celery_app.conf.broker_transport_options.update(
            socket_connect_timeout=3, socket_timeout=3,
        )
        result = run_probe(celery_app, args.queue, args.nonce, args.hostname)
    except Exception:
        print("worker queue probe failed", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
