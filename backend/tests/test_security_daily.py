from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.security_daily import (
    FileSecurityDailyControl,
    SecurityDailyConfiguration,
    SecurityDailyControlResult,
    SecurityDailyDeliveryRequest,
    SecurityDailyOverview,
    SecurityDailyPage,
    SecurityDailyQuery,
    SecurityDailyReportRecord,
    SecurityDailyService,
    SecurityDailyValidationError,
    _timeline,
    resolve_configuration_state,
    validate_security_daily_payload,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def payload() -> dict[str, object]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("status", ["normal", "attention", "high"])
def test_security_daily_payload_accepts_all_supported_statuses(status: str) -> None:
    value = payload()
    value["status"] = status

    result = validate_security_daily_payload(value)

    assert result["status"] == status
    assert len(result["metrics"]) == 5


def test_security_daily_payload_rejects_unknown_fields_and_sensitive_text() -> None:
    unknown = payload()
    unknown["unexpected"] = "must fail"
    with pytest.raises(SecurityDailyValidationError):
        validate_security_daily_payload(unknown)

    sensitive = payload()
    sensitive["summary"] = "phone=13800138000"
    with pytest.raises(SecurityDailyValidationError):
        validate_security_daily_payload(sensitive)

    credential = payload()
    credential["summary"] = "Authorization: Bearer re_secret_value"
    with pytest.raises(SecurityDailyValidationError):
        validate_security_daily_payload(credential)

    raw_body = payload()
    raw_body["metrics"][0]["content"] = "短信正文不得进入日报"
    with pytest.raises(SecurityDailyValidationError):
        validate_security_daily_payload(raw_body)


@pytest.mark.asyncio
async def test_file_control_writes_only_redacted_request_and_reads_result(tmp_path: Path) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir(mode=0o700)
    config_dir = tmp_path / "config"
    request = SecurityDailyDeliveryRequest(
        request_id=uuid4(),
        report_date=date(2026, 7, 15),
        action="send",
        state="pending",
        requested_at=datetime(2026, 7, 16, 8, tzinfo=SHANGHAI),
        idempotent=False,
    )
    control = FileSecurityDailyControl(control_dir, config_dir)

    await control.sync_configuration(
        SecurityDailyConfiguration(
            enabled=True,
            api_key="re_test_value",
            recipients=("security-owner@example.com",),
        )
    )
    config_path = config_dir / "resend.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "api_key": "re_test_value",
        "recipients": ["security-owner@example.com"],
    }

    await control.submit(request, payload())
    request_path = control_dir / "requests" / f"{request.request_id}.json"
    encoded = request_path.read_text(encoding="utf-8")
    assert "resend" not in encoded.casefold()
    assert "recipient" not in encoded.casefold()
    assert "13800138000" not in encoded
    assert json.loads(encoded)["request_id"] == str(request.request_id)

    result_path = control_dir / "results" / f"{request.request_id}.json"
    result_path.write_text(
        json.dumps(
            {
                "request_id": str(request.request_id),
                "report_date": "2026-07-15",
                "state": "sent",
                "completed_at": "2026-07-16T08:02:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    result = await control.result(request.request_id)
    assert result is not None
    assert result.state == "sent"


class FakeRepository:
    def __init__(self, record: SecurityDailyReportRecord) -> None:
        self.record = record
        self.failed: list[tuple[UUID, str]] = []
        self.requests: list[SecurityDailyDeliveryRequest] = []

    async def configuration(self) -> SecurityDailyConfiguration:
        return SecurityDailyConfiguration(
            enabled=True,
            api_key="re_test_value",
            recipients=("security-owner@example.com",),
        )

    async def update_configuration(
        self,
        update: object,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyConfiguration:
        raise NotImplementedError

    async def overview(self, *, now: datetime) -> SecurityDailyOverview:
        return SecurityDailyOverview(
            enabled=True,
            configuration_state="ready",
            schedule_time="08:00",
            timezone="Asia/Shanghai",
            period_description="汇总前一上海自然日",
            last_generated_at=self.record.generated_at,
            last_delivered_at=None,
            next_scheduled_at=now,
            latest_failure=None,
            delivery_status=self.record.delivery_status,
            recipient_count=1,
            resend_configured=True,
            sender_domain="reports.neuer.cn",
            sender_address="security-daily@reports.neuer.cn",
            beat_restart_required=True,
        )

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        return SecurityDailyPage((self.record,), 1, query.page, query.page_size)

    async def get_report(self, report_date: date) -> SecurityDailyReportRecord | None:
        return self.record if report_date == self.record.report_date else None

    async def request_delivery(
        self,
        record: SecurityDailyReportRecord,
        action: str,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyDeliveryRequest:
        request = SecurityDailyDeliveryRequest(
            request_id=uuid4(),
            report_date=record.report_date,
            action=action,  # type: ignore[arg-type]
            state="pending",
            requested_at=datetime.now(SHANGHAI),
            idempotent=False,
        )
        self.requests.append(request)
        return request

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]:
        return ()

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None:
        return None

    async def mark_request_failed(self, request_id: UUID, message: str) -> None:
        self.failed.append((request_id, message))


class FakeControl:
    def __init__(self) -> None:
        self.submitted: list[tuple[SecurityDailyDeliveryRequest, dict[str, object]]] = []
        self.synced: list[SecurityDailyConfiguration] = []

    async def sync_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        self.synced.append(configuration)

    async def submit(
        self, request: SecurityDailyDeliveryRequest, report: dict[str, object]
    ) -> None:
        self.submitted.append((request, report))

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None:
        return None


def record() -> SecurityDailyReportRecord:
    return SecurityDailyReportRecord(
        id=1,
        report_date=date(2026, 7, 15),
        period_start=datetime(2026, 7, 15, 0, tzinfo=SHANGHAI),
        period_end=datetime(2026, 7, 15, 23, 59, 59, tzinfo=SHANGHAI),
        status="attention",
        generation_status="ready",
        delivery_status="not_sent",
        generated_at=datetime(2026, 7, 16, 8, tzinfo=SHANGHAI),
        delivered_at=None,
        recipient_count=1,
        retry_count=0,
        last_error=None,
        last_error_at=None,
        updated_at=datetime(2026, 7, 16, 8, tzinfo=SHANGHAI),
        payload=payload(),
    )


def test_unavailable_report_timeline_does_not_claim_delivery_failed() -> None:
    unavailable = replace(
        record(),
        generation_status="unavailable",
        last_error="安全日报证据源不可用",
        last_error_at=datetime(2026, 7, 16, 8, 1, tzinfo=SHANGHAI),
        payload=None,
    )

    events = _timeline(unavailable)

    assert events[-1]["type"] == "evidence_unavailable"
    assert events[-1]["label"] == "证据不可用"


@pytest.mark.asyncio
async def test_service_submits_redacted_report_without_mail_credentials() -> None:
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(repository, control)
    principal = SecurityPrincipal(1, 2, "admin", "security", "admin")

    request = await service.request_delivery(
        date(2026, 7, 15),
        "send",
        principal=principal,
        ip="127.0.0.1",
    )

    assert request.state == "pending"
    assert len(control.submitted) == 1
    assert control.synced[0].api_key == "re_test_value"
    encoded = json.dumps(control.submitted[0][1], ensure_ascii=False)
    assert "secret" not in encoded.casefold()
    assert "recipient" not in encoded.casefold()


@pytest.mark.parametrize(
    ("enabled", "resend_configured", "recipient_count", "expected"),
    [
        (False, False, 0, "disabled"),
        (True, False, 0, "dispatcher_missing"),
        (True, True, 0, "recipients_empty"),
        (True, True, 1, "ready"),
    ],
)
def test_configuration_state_explains_current_security_daily_readiness(
    enabled: bool,
    resend_configured: bool,
    recipient_count: int,
    expected: str,
) -> None:
    assert (
        resolve_configuration_state(
            enabled=enabled,
            resend_configured=resend_configured,
            recipient_count=recipient_count,
        )
        == expected
    )
