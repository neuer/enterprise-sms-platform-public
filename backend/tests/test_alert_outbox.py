from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.alert import AlertEvent, AlertRouting, SmtpRouting
from app.services.outbox import OutboxClaim
from app.tasks import outbox as tasks
from tests.test_outbox import FakeRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["wecom", "smtp"])
@pytest.mark.parametrize("outcome", ["success", "failure", "duplicate", "unconfigured"])
async def test_alert_delivery_uses_execution_claim_and_persists_failure_for_retry(
    channel: str, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, lease_id = uuid4(), uuid4()
    outbox = FakeRepository()
    if outcome != "duplicate":
        outbox.claim = OutboxClaim(event_id, lease_id, "alert.delivery", (7, channel))
    calls: list[AlertEvent] = []

    class AlertRepository:
        async def load_event(self, alert_id: int) -> dict[str, object]:
            assert alert_id == 7
            return {
                "alert_type": "probe", "level": "warn", "title": "合成告警",
                "detail": {"count": 1}, "dedup_key": "probe",
            }

        async def load_routing(self) -> AlertRouting:
            if outcome == "unconfigured":
                return AlertRouting("", None)
            return AlertRouting(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic",
                SmtpRouting("mail.internal", 25, "sms@internal", ("ops@internal",)),
            )

    async def send(_self: object, _route: object, event: AlertEvent) -> None:
        calls.append(event)
        if outcome == "failure":
            raise TimeoutError("synthetic channel unavailable")

    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(
        alert_smtp_allowed_host_set={"mail.internal"},
    ))
    monkeypatch.setattr(tasks, "SqlOutboxRepository", lambda _settings: outbox)
    monkeypatch.setattr(tasks, "SqlAlertRepository", lambda _settings: AlertRepository())
    monkeypatch.setattr(tasks.WeComChannel, "send", send)
    monkeypatch.setattr(tasks.SmtpChannel, "send", send)

    if outcome == "failure":
        with pytest.raises(TimeoutError):
            await tasks._deliver_alert(7, channel, str(event_id))
        assert ("execution_failed", (event_id, lease_id, "TimeoutError")) in outbox.events
        assert not any(name == "complete" for name, _ in outbox.events)
    else:
        result = await tasks._deliver_alert(7, channel, str(event_id))
        assert result == (1 if outcome == "success" else 0)
        if outcome != "duplicate":
            assert ("complete", (event_id, lease_id)) in outbox.events
    assert len(calls) == (1 if outcome in {"success", "failure"} else 0)
