from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import app.services.alert_repository as alert_repository_module
from app.services.alert_repository import SqlAlertRepository
from app.services.sensitive_config import AlertCredentialCipher, encrypt_wecom_webhook
from app.settings import Settings

PRIVATE_KEY = X25519PrivateKey.from_private_bytes(b"w" * 32)
PUBLIC_CIPHER = AlertCredentialCipher(public_key=PRIVATE_KEY.public_key())
PRIVATE_CIPHER = AlertCredentialCipher(private_key=PRIVATE_KEY)


def protected_webhook(value: str) -> str:
    return encrypt_wecom_webhook(value, PUBLIC_CIPHER)


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalar: object = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one_or_none(self) -> object:
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def bind(repository: SqlAlertRepository, connection: FakeConnection) -> FakeEngine:
    engine = FakeEngine(connection)
    repository._engine = lambda: engine  # type: ignore[method-assign]
    return engine


@pytest.mark.asyncio
async def test_channel_availability_uses_boolean_security_boundary() -> None:
    repository = SqlAlertRepository(credential_cipher=PRIVATE_CIPHER)
    connection = FakeConnection(
        [FakeResult(rows=[{"wecom_configured": True, "smtp_configured": False}])]
    )
    bind(repository, connection)

    assert await repository.load_channel_availability() == (True, False)
    sql, _params = connection.calls[0]
    assert "alert_channel_availability()" in sql
    assert "alert_wecom_webhook" not in sql


@pytest.mark.asyncio
async def test_routing_uses_empty_channels_as_log_sink_only() -> None:
    repository = SqlAlertRepository()
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {"key": "alert_wecom_webhook", "value": ""},
                    {"key": "alert_mail_to", "value": ""},
                    {"key": "alert_smtp_host", "value": "mail.internal"},
                    {"key": "alert_smtp_port", "value": "25"},
                    {"key": "alert_mail_from", "value": "sms@internal"},
                ]
            )
        ]
    )
    bind(repository, connection)

    routing = await repository.load_routing()

    assert routing.wecom_webhook == ""
    assert routing.smtp is None


@pytest.mark.asyncio
async def test_routing_parses_recipients_and_smtp_relay() -> None:
    repository = SqlAlertRepository(
        Settings.model_validate(
            {
                "debug": True,
                "auth_mock": True,
                "alert_smtp_allowed_hosts": "mail.internal",
            }
        ),
        credential_cipher=PRIVATE_CIPHER,
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "key": "alert_wecom_webhook",
                        "value": protected_webhook(
                            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token"
                        ),
                    },
                    {"key": "alert_mail_to", "value": "a@x, b@y"},
                    {"key": "alert_smtp_host", "value": "mail.internal"},
                    {"key": "alert_smtp_port", "value": "2525"},
                    {"key": "alert_mail_from", "value": "sms@internal"},
                ]
            )
        ]
    )
    bind(repository, connection)

    routing = await repository.load_routing()

    assert (
        routing.wecom_webhook
        == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token"
    )
    assert routing.smtp is not None
    assert routing.smtp.recipients == ("a@x", "b@y")
    assert routing.smtp.port == 2525


@pytest.mark.asyncio
async def test_routing_rejects_historical_non_allowlisted_destinations() -> None:
    repository = SqlAlertRepository(
        Settings.model_validate(
            {
                "debug": True,
                "auth_mock": True,
                "alert_smtp_allowed_hosts": "mail.internal",
            }
        ),
        credential_cipher=PRIVATE_CIPHER,
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {"key": "alert_wecom_webhook", "value": "https://attacker.example/hook"},
                    {"key": "alert_mail_to", "value": "ops@internal"},
                    {"key": "alert_smtp_host", "value": "attacker.internal"},
                    {"key": "alert_smtp_port", "value": "25"},
                    {"key": "alert_mail_from", "value": "sms@internal"},
                ]
            )
        ]
    )
    bind(repository, connection)

    routing = await repository.load_routing()

    assert routing.wecom_webhook == ""
    assert routing.smtp is None


@pytest.mark.asyncio
async def test_invalid_smtp_port_degrades_to_log_sink_routing() -> None:
    repository = SqlAlertRepository(credential_cipher=PRIVATE_CIPHER)
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {"key": "alert_mail_to", "value": "ops@internal"},
                    {"key": "alert_smtp_port", "value": "invalid"},
                ]
            )
        ]
    )
    bind(repository, connection)

    assert (await repository.load_routing()).smtp is None


@pytest.mark.asyncio
async def test_claim_is_atomic_four_hour_dedup_and_does_not_persist_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlAlertRepository(credential_cipher=PRIVATE_CIPHER)
    connection = FakeConnection([FakeResult(scalar=42)])
    engine = bind(repository, connection)
    outbox_events: list[Any] = []

    async def outbox(_connection: object, spec: Any, **_: object) -> object:
        outbox_events.append(spec)
        return object()

    monkeypatch.setattr(alert_repository_module, "enqueue_outbox", outbox)

    claimed = await repository.claim(
        alert_type="balance_low",
        level="warn",
        title="余额低",
        detail={"balance": 5000},
        channels="log-sink,wecom",
        dedup_key="balance_low",
        dedup_hours=4,
    )

    assert claimed == 42
    sql, params = connection.calls[0]
    assert "RETURNING id" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "make_interval(hours => :dedup_hours)" in sql
    assert "WHERE dedup_key = :dedup_key" in sql
    assert "webhook" not in str(params).casefold()
    assert params["detail"] == '{"balance": 5000}'
    assert params["dedup_hours"] == 4
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == "alert.delivery"
    assert outbox_events[0].args == (42, "wecom")
    assert engine.disposed
