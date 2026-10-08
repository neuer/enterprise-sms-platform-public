"""官方一次性 Redis 7 验证职责 ACL、Kombu 确认恢复和跨队列拒绝。"""

from __future__ import annotations

import importlib.util
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from kombu import Connection, Exchange, Producer, Queue
from redis import Redis
from redis.exceptions import AuthenticationError, NoPermissionError

from app.core.broker_authorization import WORKER_QUEUES, broker_transport_options

pytestmark = [
    pytest.mark.authorization,
    pytest.mark.skipif(
        not os.environ.get("SMS_ISOLATED_TEST_DATABASE")
        or not os.environ.get("AUTH_GUARD_REDIS_URL"),
        reason="requires the official disposable Redis test container",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def broker():
    url = os.environ["AUTH_GUARD_REDIS_URL"]
    assert urlsplit(url).hostname == "127.0.0.1", "ACL probe must use disposable loopback Redis"
    spec = importlib.util.spec_from_file_location(
        "broker_secret_material",
        ROOT / "deploy/scripts/prepare_runtime_secrets.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    material = module.broker_runtime_material(secrets.token_urlsafe(48).encode())
    admin = Redis.from_url(url, decode_responses=True, socket_timeout=3)
    clients = {}
    users = []
    try:
        for line in material[module.BROKER_ACL_NAME].decode().splitlines():
            parts = re.findall(r"\([^)]*\)|\S+", line)
            if parts[1] == "default":
                continue  # 保留官方隔离容器的测试管理连接，不修改其他故障域。
            username = parts[1]
            users.append(username)
            admin.execute_command("ACL", "SETUSER", username, "reset", *parts[2:])
        for role in module.BROKER_ROLES:
            clients[role] = Redis.from_url(
                url,
                username=f"sms_broker_{role}",
                password=material[f"redis_broker_{role}_password"].decode(),
                decode_responses=True,
                socket_timeout=3,
            )
            assert clients[role].ping()
        yield admin, clients, material
    finally:
        for client in clients.values():
            client.close()
        if users:
            admin.execute_command("ACL", "DELUSER", *users)
        keys = []
        for role, queue in WORKER_QUEUES.items():
            keys.extend(
                [
                    queue,
                    f"_kombu.binding.{queue}",
                    f"unacked:{role}",
                    f"unacked_index:{role}",
                    f"unacked_mutex:{role}",
                    f"result:{role}:probe",
                ]
            )
        if keys:
            admin.delete(*keys)
        admin.close()


def test_real_redis_worker_credentials_cannot_cross_capabilities(broker) -> None:
    admin, clients, material = broker
    for role, queue in WORKER_QUEUES.items():
        client = clients[role]
        assert client.lpush(queue, "synthetic-reference") >= 1
        assert client.rpop(queue) == "synthetic-reference"
        assert client.hset(f"unacked:{role}", "synthetic", "reference") in (0, 1)
        assert client.hget(f"unacked:{role}", "synthetic") == "reference"
        client.hdel(f"unacked:{role}", "synthetic")
        client.set(f"result:{role}:probe", "synthetic-result")
        assert client.get(f"result:{role}:probe") == "synthetic-result"
        for other, other_queue in WORKER_QUEUES.items():
            if other == role:
                continue
            with pytest.raises(NoPermissionError):
                client.lpush(other_queue, "must-not-be-enqueued")
            with pytest.raises(NoPermissionError):
                client.rpop(other_queue)
            with pytest.raises(NoPermissionError):
                client.hset(f"unacked:{other}", "forged", "must-not-be-restored")
            with pytest.raises(NoPermissionError):
                client.get(f"result:{other}:probe")
        for command in [
            ("SET", "unacked", "legacy-forgery"),
            ("GET", "auth:jwt:revoked:probe"),
            ("SADD", "_kombu.binding.celery.pidbox", "control"),
            ("PUBLISH", "celery.pidbox", "control"),
            ("KEYS", "*"),
            ("CONFIG", "GET", "maxmemory"),
            ("FLUSHALL",),
            ("CLIENT", "PAUSE", 1),
        ]:
            with pytest.raises(NoPermissionError):
                client.execute_command(*command)
    # 偷到 callback 的凭据，不能只改用户名就成为生产者或发送消费者。
    for other in ("realtime", "report", "bulk", "beat", "dispatcher"):
        impostor = Redis.from_url(
            os.environ["AUTH_GUARD_REDIS_URL"],
            username=f"sms_broker_{other}",
            password=material["redis_broker_callback_password"].decode(),
            socket_timeout=3,
        )
        try:
            with pytest.raises(AuthenticationError):
                impostor.ping()
        finally:
            impostor.close()
    assert admin.hlen("unacked:realtime") == 0


def test_real_redis_publishers_cannot_consume_or_rewrite_confirmation_state(broker) -> None:
    _, clients, _ = broker
    for role in ("beat", "dispatcher"):
        client = clients[role]
        for queue in WORKER_QUEUES.values():
            client.sadd(f"_kombu.binding.{queue}", "synthetic-binding")
            client.lpush(queue, "synthetic-reference")
            with pytest.raises(NoPermissionError):
                client.rpop(queue)
        with pytest.raises(NoPermissionError):
            client.hset("unacked:callback", "forged", "reference")
        with pytest.raises(NoPermissionError):
            client.publish("celery.pidbox", "control")


def test_kombu_publication_delivery_requeue_and_ack_use_scoped_keys(broker) -> None:
    admin, _, material = broker
    parsed = urlsplit(os.environ["AUTH_GUARD_REDIS_URL"])

    def connection(role: str) -> Connection:
        return Connection(
            hostname=parsed.hostname,
            port=parsed.port,
            virtual_host="0",
            transport="redis",
            userid=f"sms_broker_{role}",
            password=material[f"redis_broker_{role}_password"].decode(),
            transport_options=broker_transport_options(role),
            connect_timeout=3,
        )

    for role, name in WORKER_QUEUES.items():
        queue = Queue(name, Exchange(name), routing_key=name)
        with connection("dispatcher") as publisher:
            Producer(publisher).publish(
                {"reference": 1},
                exchange=queue.exchange,
                routing_key=name,
                declare=[queue],
                serializer="json",
                retry=False,
            )
        with connection(role) as consumer, consumer.SimpleQueue(queue) as inbox:
            message = inbox.get(block=False)
            assert message.payload == {"reference": 1}
            assert admin.hlen(f"unacked:{role}") == 1
            message.reject(requeue=True)
            restored = inbox.get(block=False)
            assert restored.payload == {"reference": 1}
            restored.ack()
            assert admin.hlen(f"unacked:{role}") == 0
        assert admin.hlen("unacked") == 0


def test_celery_beat_and_dispatcher_publish_without_result_subscription(broker) -> None:
    from app.tasks import celery_app, register_task_modules

    register_task_modules()
    assert celery_app.conf.task_ignore_result is True
    _, _, material = broker
    parsed = urlsplit(os.environ["AUTH_GUARD_REDIS_URL"])
    tasks = {
        "realtime": "app.tasks.poll_balance",
        "report": "app.tasks.poll_report",
        "bulk": "app.tasks.dispatch_exports",
        "callback": "app.tasks.dispatch_callbacks",
    }

    def connection(role: str) -> Connection:
        return Connection(
            hostname=parsed.hostname,
            port=parsed.port,
            virtual_host="0",
            transport="redis",
            userid=f"sms_broker_{role}",
            password=material[f"redis_broker_{role}_password"].decode(),
            transport_options=broker_transport_options(role),
            connect_timeout=3,
        )

    for role, name in WORKER_QUEUES.items():
        queue = Queue(name, Exchange(name), routing_key=name)
        for publisher_role in ("beat", "dispatcher"):
            with connection(publisher_role) as publisher:
                if publisher_role == "beat":
                    # 与 beat 相同：使用注册任务的 apply_async，不能依赖额外结果订阅权限。
                    celery_app.tasks[tasks[role]].apply_async(queue=name, connection=publisher)
                else:
                    celery_app.send_task(
                        tasks[role],
                        args=[],
                        queue=name,
                        connection=publisher,
                        ignore_result=True,
                    )
            with connection(role) as consumer, consumer.SimpleQueue(queue) as inbox:
                message = inbox.get(block=False)
                assert message.headers["task"] == tasks[role]
                assert message.delivery_info["routing_key"] == name
                message.ack()
