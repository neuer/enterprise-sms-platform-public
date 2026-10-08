"""职责队列探测必须由真实消费者响应，且不泄漏连接信息或复用旧挑战。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import worker_probe as module

NONCE = "a" * 32
HOSTNAME = "celery@worker-test"


class FakeClient:
    def __init__(self, reply: bytes | None = None) -> None:
        self.reply = reply
        self.deleted: list[bytes] = []
        self.writes: list[tuple[bytes, int, str]] = []

    def delete(self, key: bytes) -> None:
        self.deleted.append(key)

    def get(self, key: bytes) -> bytes | None:
        return self.reply

    def setex(self, key: bytes, ttl: int, value: str) -> None:
        self.writes.append((key, ttl, value))


def fake_app(client: FakeClient) -> Any:
    sent: list[tuple[str, dict[str, object]]] = []
    backend = SimpleNamespace(
        client=client,
        get_key_for_task=lambda task_id: ("result:callback:" + task_id).encode(),
    )
    return SimpleNamespace(
        backend=backend,
        sent=sent,
        send_task=lambda name, **kwargs: sent.append((name, kwargs)),
    )


@pytest.mark.parametrize("queue", sorted(module.PROBE_QUEUES))
def test_bound_probe_response_and_ephemeral_cleanup(queue: str) -> None:
    expected = module.expected_reply(queue, NONCE, HOSTNAME)
    client = FakeClient(json.dumps(expected).encode())
    app = fake_app(client)
    assert module.run_probe(app, queue, NONCE, HOSTNAME) == expected
    name, options = app.sent[0]
    assert name == module.PROBE_TASK
    assert options == {
        "args": (NONCE,),
        "queue": queue,
        "exchange": queue,
        "routing_key": queue,
        "expires": 30,
        "ignore_result": True,
        "retry": False,
        "delivery_mode": 1,
    }
    assert len(client.deleted) == 2 and client.deleted[0] == client.deleted[1]


@pytest.mark.parametrize(
    "field", ["nonce", "worker", "active_queues", "queue", "exchange", "routing_key"]
)
def test_wrong_response_is_rejected_and_cleaned(field: str) -> None:
    reply = module.expected_reply("callback", NONCE, HOSTNAME)
    reply[field] = "mismatched"
    client = FakeClient(json.dumps(reply).encode())
    with pytest.raises(ValueError, match="binding"):
        module.run_probe(fake_app(client), "callback", NONCE, HOSTNAME)
    assert len(client.deleted) == 2


@pytest.mark.parametrize("raw", [b"not-json", b"x" * 4097, b"[]", b"null"])
def test_malformed_or_oversized_response_fails(raw: bytes) -> None:
    with pytest.raises(ValueError):
        module.run_probe(fake_app(FakeClient(raw)), "callback", NONCE, HOSTNAME)


def test_no_consumer_cannot_be_treated_as_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    ticks = iter([0.0, 0.1, 11.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError):
        module.run_probe(fake_app(client), "callback", NONCE, HOSTNAME)
    assert len(client.deleted) == 2


def test_broker_failure_is_not_a_success() -> None:
    client = FakeClient()
    app = fake_app(client)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic private DSN")

    app.send_task = fail
    with pytest.raises(RuntimeError):
        module.run_probe(app, "callback", NONCE, HOSTNAME)
    assert len(client.deleted) == 2


@pytest.mark.parametrize(
    "role,queue",
    [
        ("realtime", "realtime"),
        ("report", "realtime-report"),
        ("bulk", "bulk"),
        ("callback", "callback"),
    ],
)
def test_consumer_response_proves_its_own_queue(role: str, queue: str) -> None:
    client = FakeClient()
    app = fake_app(client)
    app.amqp = SimpleNamespace(
        queues=SimpleNamespace(
            consume_from={
                queue: SimpleNamespace(routing_key=queue, exchange=SimpleNamespace(name=queue)),
            }
        )
    )
    task = SimpleNamespace(
        app=app,
        request=SimpleNamespace(
            hostname=HOSTNAME,
            delivery_info={"routing_key": queue},
        ),
    )
    module.consume_probe(task, NONCE, role)
    key, ttl, value = client.writes[0]
    assert key.endswith(NONCE.encode()) and ttl == 30
    assert json.loads(value) == module.expected_reply(queue, NONCE, HOSTNAME)


@pytest.mark.parametrize("kind", ["extra_queue", "exchange", "routing_key", "delivery", "producer"])
def test_consumer_rejects_misbinding_without_writing(kind: str) -> None:
    client = FakeClient()
    app = fake_app(client)
    queue = SimpleNamespace(routing_key="callback", exchange=SimpleNamespace(name="callback"))
    app.amqp = SimpleNamespace(queues=SimpleNamespace(consume_from={"callback": queue}))
    task = SimpleNamespace(
        app=app,
        request=SimpleNamespace(
            hostname=HOSTNAME,
            delivery_info={"routing_key": "callback"},
        ),
    )
    if kind == "extra_queue":
        app.amqp.queues.consume_from["bulk"] = queue
    elif kind == "exchange":
        queue.exchange.name = "bulk"
    elif kind == "routing_key":
        queue.routing_key = "bulk"
    elif kind == "delivery":
        task.request.delivery_info["routing_key"] = "bulk"
    with pytest.raises(ValueError):
        module.consume_probe(task, NONCE, "beat" if kind == "producer" else "callback")
    assert not client.writes


@pytest.mark.parametrize(
    "queue,nonce,hostname",
    [
        ("unknown", NONCE, HOSTNAME),
        ("callback", "bad", HOSTNAME),
        ("callback", NONCE, "not-a-celery-node"),
    ],
)
def test_invalid_bindings_never_publish(queue: str, nonce: str, hostname: str) -> None:
    app = fake_app(FakeClient())
    with pytest.raises(ValueError):
        module.run_probe(app, queue, nonce, hostname)
    assert not app.sent


@pytest.mark.parametrize("mode", ["success", "wrong_role", "connection_error"])
def test_cli_prints_only_validated_status_and_restores_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    from app import settings as settings_module
    from app import tasks

    previous = module.logging.root.manager.disable
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "probe",
            "--queue",
            "callback",
            "--nonce",
            NONCE,
            "--hostname",
            HOSTNAME,
        ],
    )
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            sms_component="worker",
            redis_broker_role="bulk" if mode == "wrong_role" else "callback",
        ),
    )
    app = fake_app(
        FakeClient(json.dumps(module.expected_reply("callback", NONCE, HOSTNAME)).encode())
    )
    app.conf = SimpleNamespace(update=lambda **_: None, broker_transport_options={})
    if mode == "connection_error":

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic credential that must not be printed")

        app.send_task = fail
    monkeypatch.setattr(tasks, "celery_app", app)
    assert module.main() == (0 if mode == "success" else 1)
    output = capsys.readouterr()
    if mode == "success":
        assert json.loads(output.out) == module.expected_reply("callback", NONCE, HOSTNAME)
        assert not output.err
    else:
        assert output.out == "" and output.err == "worker queue probe failed\n"
    assert module.logging.root.manager.disable == previous
