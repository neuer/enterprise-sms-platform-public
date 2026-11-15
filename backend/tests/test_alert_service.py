from __future__ import annotations

import io
import smtplib
import time
from typing import Any
from uuid import UUID

import pytest

import app.core.jobtrack as jobtrack_module
import app.services.alert as alert_module
import app.services.uncertain_repository as uncertain_repository_module
import app.tasks.poll_balance as poll_balance_module
from app.services.alert import AlertService
from app.services.alert_repository import SqlAlertService


class FakeRepository:
    def __init__(self, availability: tuple[bool, bool], *, claimed_id: int | None = 7) -> None:
        self.availability = availability
        self.claimed_id = claimed_id
        self.claims: list[dict[str, Any]] = []

    async def load_channel_availability(self) -> tuple[bool, bool]:
        return self.availability

    async def claim(self, **values: Any) -> int | None:
        self.claims.append(values)
        return self.claimed_id


EMPTY = (False, False)


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
        await AlertService(repository).emit(
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
        await AlertService(repository).emit(
            alert_type="unsafe",
            level="warn",
            title=title,
            detail={"count": 1},
            dedup_key=dedup_key,
        )

    assert repository.claims == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["batch_no", "stream_id", "custom_id"])
async def test_business_reference_does_not_exempt_plain_phone(field: Any) -> None:
    repository = FakeRepository(EMPTY)
    key, detail = alert_module.alert_reference(field, "1" + "2" * 10)
    with pytest.raises(ValueError, match="PII"):
        await AlertService(repository).emit(
            alert_type="unsafe",
            level="warn",
            title="引用校验",
            detail=detail,
            dedup_key=key,
        )
    assert repository.claims == []


@pytest.mark.parametrize("value", ["batch-1", "BATCH-8", "abcdefab-cdef-abcd-efab-cdefabcdefab"])
def test_ordinary_alert_reference_keeps_existing_metadata_and_dedup(value: str) -> None:
    assert alert_module.alert_reference("batch_no", value) == (value, {"batch_no": value})


def test_encoded_alert_reference_preserves_uuid_identity() -> None:
    identifiers = [UUID(hex="f" * 20 + "1" + digit * 10 + "a") for digit in ("2", "3")]
    references = [alert_module.alert_reference("stream_id", str(value))[0] for value in identifiers]
    assert len(set(references)) == 2
    for original, reference in zip(identifiers, references, strict=True):
        decoded = reference.removeprefix("uuid-alpha:").translate(
            str.maketrans("ghijklmnop", "0123456789")
        )
        assert UUID(decoded) == original
        assert not any(char.isdigit() for char in reference)


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


async def test_emit_alert_best_effort_skips_missing_sink_and_logs_failure_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    class FailingSink:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def emit(self, **values: Any) -> None:
            self.calls.append(values)
            raise RuntimeError("alert storage unavailable")

    logger = logging.getLogger("tests.alert_best_effort")
    event = {
        "alert_type": "probe",
        "level": "warn",
        "title": "probe",
        "detail": {"source": "report"},
        "dedup_key": "probe:report",
    }
    await alert_module.emit_alert_best_effort(
        None, logger=logger, log_message="probe alert unavailable", log_extra={}, **event
    )
    sink = FailingSink()
    with caplog.at_level(logging.ERROR, logger="tests.alert_best_effort"):
        await alert_module.emit_alert_best_effort(
            sink,
            logger=logger,
            log_message="probe alert unavailable",
            log_extra={"source": "report"},
            **event,
        )

    assert sink.calls == [event]
    [record] = caplog.records
    assert record.getMessage() == "probe alert unavailable"
    assert record.__dict__["source"] == "report"
    assert record.__dict__["error_type"] == "RuntimeError"
    assert "alert storage unavailable" not in caplog.text


class RememberingRepository:
    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []

    async def load_channel_availability(self) -> tuple[bool, bool]:
        return EMPTY

    async def claim(self, **values: Any) -> int | None:
        self.claims.append(values)
        seen = [item["dedup_key"] for item in self.claims]
        if seen.count(values["dedup_key"]) > 1:
            return None
        return len(self.claims)


@pytest.mark.asyncio
async def test_same_window_key_dedups_and_new_shanghai_day_key_claims_again() -> None:
    repository = RememberingRepository()
    service = AlertService(repository)

    await service.emit(
        alert_type="anomaly",
        level="crit",
        title="验证码发送量异常",
        detail={"recommendation": "建议：核查该应用调用来源，必要时停用 API Key 或轮换。"},
        dedup_key="anomaly:7:verify:2026-10-22",
        dedup_hours=24,
    )
    await service.emit(
        alert_type="anomaly",
        level="crit",
        title="验证码发送量异常",
        detail={"recommendation": "建议：核查该应用调用来源，必要时停用 API Key 或轮换。"},
        dedup_key="anomaly:7:verify:2026-10-22",
        dedup_hours=24,
    )
    await service.emit(
        alert_type="anomaly",
        level="warn",
        title="发送量异常",
        detail={"category": "notice"},
        dedup_key="anomaly:7:notice:2026-10-23",
        dedup_hours=24,
    )

    assert [item["dedup_key"] for item in repository.claims] == [
        "anomaly:7:verify:2026-10-22",
        "anomaly:7:verify:2026-10-22",
        "anomaly:7:notice:2026-10-23",
    ]
    assert {item["dedup_hours"] for item in repository.claims} == {24}


@pytest.fixture(autouse=True)
def forbid_direct_channel_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("alert producers must only persist Outbox events")

    monkeypatch.setattr(alert_module.WeComChannel, "send", unexpected)
    monkeypatch.setattr(alert_module.SmtpChannel, "send", unexpected)


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_id", [None, 7])
@pytest.mark.parametrize(
    ("availability", "channels"),
    [
        ((False, False), "log-sink"),
        ((True, False), "log-sink,wecom"),
        ((False, True), "log-sink,smtp"),
        ((True, True), "log-sink,wecom,smtp"),
    ],
)
async def test_emit_records_configured_channels_without_credentials_or_direct_delivery(
    availability: tuple[bool, bool], channels: str, claimed_id: int | None
) -> None:
    repository = FakeRepository(availability, claimed_id=claimed_id)
    await AlertService(repository).emit(
        alert_type="balance_low",
        level="warn",
        title="短信余额低",
        detail={"balance": 5000, "threshold": 10000},
        dedup_key="balance_low",
    )
    assert repository.claims == [{
        "alert_type": "balance_low",
        "level": "warn",
        "title": "短信余额低",
        "detail": {"balance": 5000, "threshold": 10000},
        "channels": channels,
        "dedup_key": "balance_low",
        "dedup_hours": 4,
    }]
