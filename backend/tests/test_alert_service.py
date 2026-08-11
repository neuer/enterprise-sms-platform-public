from __future__ import annotations

import io
import smtplib
import time
from typing import Any

import pytest

import app.core.jobtrack as jobtrack_module
import app.services.alert as alert_module
import app.services.uncertain_repository as uncertain_repository_module
import app.tasks.poll_balance as poll_balance_module
from app.services.alert import AlertRouting, AlertService, SmtpRouting
from app.services.alert_repository import SqlAlertService


class FakeRepository:
    def __init__(self, routing: AlertRouting, *, claimed_id: int | None = 7) -> None:
        self.routing = routing
        self.claimed_id = claimed_id
        self.claims: list[dict[str, Any]] = []

    async def load_routing(self) -> AlertRouting:
        return self.routing

    async def claim(self, **values: Any) -> int | None:
        self.claims.append(values)
        return self.claimed_id


class FakeWeCom:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []

    async def send(self, webhook: str, event: Any) -> None:
        self.calls.append((webhook, event))
        if self.fail:
            raise RuntimeError("wecom unavailable")


class FakeSmtp:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[SmtpRouting, Any]] = []

    async def send(self, routing: SmtpRouting, event: Any) -> None:
        self.calls.append((routing, event))
        if self.fail:
            raise RuntimeError("smtp unavailable")


EMPTY = AlertRouting(wecom_webhook="", smtp=None)


@pytest.mark.asyncio
async def test_empty_channel_config_persists_log_sink_without_external_calls() -> None:
    repository = FakeRepository(EMPTY)
    wecom = FakeWeCom()
    smtp = FakeSmtp()
    service = AlertService(repository, wecom=wecom, smtp=smtp)

    await service.emit(
        alert_type="balance_low",
        level="warn",
        title="短信余额低",
        detail={"balance": 5000, "threshold": 10000},
        dedup_key="balance_low",
    )

    assert repository.claims[0]["channels"] == "log-sink"
    assert repository.claims[0]["dedup_hours"] == 4
    assert wecom.calls == []
    assert smtp.calls == []


@pytest.mark.asyncio
async def test_invalid_historical_egress_config_degrades_to_log_sink() -> None:
    repository = FakeRepository(
        AlertRouting(
            wecom_webhook="https://attacker.example/hook",
            smtp=SmtpRouting("attacker.internal", 25, "sms@internal", ("ops@internal",)),
        )
    )
    wecom = FakeWeCom()
    smtp = FakeSmtp()

    await AlertService(
        repository,
        wecom=wecom,
        smtp=smtp,
        allowed_smtp_hosts={"mail.internal"},
    ).emit(
        alert_type="unsafe_route",
        level="crit",
        title="非法告警路由",
        detail={"count": 1},
        dedup_key="unsafe_route",
    )

    assert repository.claims[0]["channels"] == "log-sink"
    assert wecom.calls == []
    assert smtp.calls == []


@pytest.mark.asyncio
async def test_duplicate_claim_never_dispatches_external_channels() -> None:
    routing = AlertRouting(
        wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token",
        smtp=SmtpRouting("mail.internal", 25, "sms@internal", ("ops@internal",)),
    )
    repository = FakeRepository(routing, claimed_id=None)
    wecom = FakeWeCom()
    smtp = FakeSmtp()

    await AlertService(
        repository,
        wecom=wecom,
        smtp=smtp,
        allowed_smtp_hosts={"mail.internal"},
    ).emit(
        alert_type="job_stalled",
        level="crit",
        title="后台任务心跳缺失",
        detail={"job_name": "poll_report"},
        dedup_key="job_stalled:poll_report",
    )

    assert repository.claims[0]["channels"] == "log-sink,wecom,smtp"
    assert wecom.calls == []
    assert smtp.calls == []


@pytest.mark.asyncio
async def test_first_claim_dispatches_both_configured_channels() -> None:
    smtp_routing = SmtpRouting("mail.internal", 2525, "sms@internal", ("ops@internal",))
    routing = AlertRouting(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token",
        smtp_routing,
    )
    repository = FakeRepository(routing)
    wecom = FakeWeCom()
    smtp = FakeSmtp()

    await AlertService(
        repository,
        wecom=wecom,
        smtp=smtp,
        allowed_smtp_hosts={"mail.internal"},
    ).emit(
        alert_type="vendor_auth_error",
        level="crit",
        title="厂商鉴权失败",
        detail={"vendor_code": 1000, "chunk_id": 3},
        dedup_key="vendor_auth_error:1000",
    )

    assert wecom.calls[0][0] == routing.wecom_webhook
    assert smtp.calls[0][0] == smtp_routing
    assert wecom.calls[0][1].detail == {"vendor_code": 1000, "chunk_id": 3}


@pytest.mark.asyncio
async def test_channel_failure_is_isolated_from_primary_workflow() -> None:
    routing = AlertRouting(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token",
        SmtpRouting("mail.internal", 25, "sms@internal", ("ops@internal",)),
    )
    smtp = FakeSmtp()

    await AlertService(
        FakeRepository(routing),
        wecom=FakeWeCom(fail=True),
        smtp=smtp,
        allowed_smtp_hosts={"mail.internal"},
    ).emit(
        alert_type="vendor_ip_error",
        level="crit",
        title="厂商 IP 校验失败",
        detail={"vendor_code": 1010},
        dedup_key="vendor_ip_error:1010",
    )

    assert len(smtp.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    [
        {"phone": "138****8000"},
        {"nested": {"mobiles": ["encrypted"]}},
        {"message": "号码 13800138000 触发失败"},
    ],
)
async def test_alert_detail_rejects_phone_fields_and_plain_numbers(detail: dict[str, Any]) -> None:
    repository = FakeRepository(EMPTY)

    with pytest.raises(ValueError, match="PII"):
        await AlertService(repository, wecom=FakeWeCom(), smtp=FakeSmtp()).emit(
            alert_type="unsafe",
            level="warn",
            title="不安全告警",
            detail=detail,
            dedup_key="unsafe",
        )

    assert repository.claims == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "dedup_key"),
    [
        ("号码 13800138000 告警", "safe"),
        ("安全标题", "unsafe:13800138000"),
    ],
)
async def test_alert_metadata_rejects_plain_phone_numbers(title: str, dedup_key: str) -> None:
    repository = FakeRepository(EMPTY)

    with pytest.raises(ValueError, match="PII"):
        await AlertService(repository, wecom=FakeWeCom(), smtp=FakeSmtp()).emit(
            alert_type="unsafe",
            level="warn",
            title=title,
            detail={"count": 1},
            dedup_key=dedup_key,
        )

    assert repository.claims == []


def test_existing_alert_producers_depend_on_unified_sql_service() -> None:
    assert vars(jobtrack_module)["SqlAlertService"] is SqlAlertService
    assert vars(poll_balance_module)["SqlAlertService"] is SqlAlertService
    assert vars(uncertain_repository_module)["SqlAlertService"] is SqlAlertService


def test_smtp_protocol_enforces_absolute_deadline_and_aggregate_reply_limit() -> None:
    class Socket:
        def settimeout(self, _timeout: float) -> None:
            return None

    expired = object.__new__(alert_module._DeadlineSmtp)
    expired._deadline = time.monotonic() - 1
    expired.sock = Socket()
    expired.file = io.BytesIO(b"250 ok\r\n")
    with pytest.raises(TimeoutError, match="absolute deadline"):
        expired.getreply()

    oversized = object.__new__(alert_module._DeadlineSmtp)
    oversized._deadline = time.monotonic() + 10
    oversized.sock = Socket()
    line = b"250-" + (b"x" * 7990) + b"\r\n"
    oversized.file = io.BytesIO(line * 9 + b"250 ok\r\n")
    with pytest.raises(smtplib.SMTPResponseException, match="Reply too large"):
        oversized.getreply()
