from __future__ import annotations

import json
import os
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
    SecurityDailyUnavailable,
    SecurityDailyValidationError,
    _count_delta_suffix,
    _enrich_day_over_day,
    _finalize_security_daily_payload,
    _problem_payload,
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
        self.delivery_failed: list[tuple[int, str]] = []
        self.ingested: list[dict[str, object]] = []
        self.config = SecurityDailyConfiguration(
            enabled=True,
            api_key="re_test_value",
            recipients=("security-owner@example.com",),
        )

    async def ingest_payload(
        self,
        payload: dict[str, object],
        *,
        recipient_count: int,
        force: bool = False,
        generation_source: str = "auto",
    ) -> bool:
        self.ingested.append(
            {
                "payload": payload,
                "recipient_count": recipient_count,
                "force": force,
                "generation_source": generation_source,
            }
        )
        self.record = replace(
            self.record,
            generation_status="ready",
            generation_source=generation_source,  # type: ignore[arg-type]
            delivery_status="not_sent",
            payload=dict(payload),
            generated_at=datetime.now(SHANGHAI),
            last_error=None,
            last_error_at=None,
        )
        return True

    async def mark_unavailable(
        self,
        report_date: date,
        *,
        period_start: object,
        period_end: object,
        reason: str,
        generation_source: str = "auto",
    ) -> bool:
        del report_date, period_start, period_end
        self.record = replace(
            self.record,
            generation_status="unavailable",
            generation_source=generation_source,  # type: ignore[arg-type]
            delivery_status="not_sent",
            payload=None,
            last_error=reason,
            last_error_at=datetime.now(SHANGHAI),
        )
        return True

    async def audit_evidence(self, period_start: object, period_end: object) -> None:
        return None

    async def configuration(self) -> SecurityDailyConfiguration:
        return self.config

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
            period_description="汇总前一自然日（北京时间）",
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

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord | None:
        return self.record if report_id == self.record.id else None

    async def get_latest_report(
        self,
        report_date: date,
        *,
        generation_source: str | None = None,
    ) -> SecurityDailyReportRecord | None:
        if report_date != self.record.report_date:
            return None
        if generation_source is not None and self.record.generation_source != generation_source:
            return None
        return self.record

    async def exists_sent_delivery(self, report_date: date) -> bool:
        return (
            report_date == self.record.report_date
            and self.record.delivery_status == "sent"
        )

    async def request_delivery(
        self,
        record: SecurityDailyReportRecord,
        action: str,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
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
        self.record = replace(self.record, delivery_status="pending")
        return request

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]:
        return ()

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None:
        return None

    async def mark_request_failed(self, request_id: UUID, message: str) -> None:
        self.failed.append((request_id, message))

    async def mark_delivery_failed(self, report_id: int, message: str) -> bool:
        self.delivery_failed.append((report_id, message))
        return True


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
        generation_source="auto",
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
        1,
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


def test_problem_payload_is_validated_and_never_fabricates_numbers() -> None:
    value = _problem_payload(
        date(2026, 7, 15),
        period_start=datetime(2026, 7, 15, 0, tzinfo=SHANGHAI),
        period_end=datetime(2026, 7, 15, 23, 59, 59, tzinfo=SHANGHAI),
        generated_at=datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        reason="采集器未产出快照",
    )

    validated = validate_security_daily_payload(value)

    assert "采集器未产出快照" in validated["summary"]
    assert all(item["value"] == "不可用" for item in validated["metrics"][:4])
    assert all(item["status"] == "缺失" for item in validated["coverage"])


def test_finalize_keeps_small_ssh_failure_count_out_of_pending_confirmation() -> None:
    value = payload()
    value["metrics"][0]["value"] = "14"
    for item in value["coverage"]:
        item["status"] = "完整"
        item["tone"] = "good"
    value["web"][3]["value"] = "0 次命中"

    finalized = _finalize_security_daily_payload(value)

    assert finalized["status"] == "normal"
    assert finalized["pending_confirmation"] == "无待确认事项。"
    assert finalized["metrics"][0]["tone"] == "neutral"
    assert finalized["metrics"][0]["note"] == "低于关注阈值，无需人工确认"
    assert not any(item["title"] == "核查 SSH 失败认证" for item in finalized["actions"])


def test_finalize_requires_confirmation_when_ssh_failures_exceed_threshold() -> None:
    value = payload()
    value["metrics"][0]["value"] = "25"
    for item in value["coverage"]:
        item["status"] = "完整"
        item["tone"] = "good"
    value["web"][3]["value"] = "0 次命中"

    finalized = _finalize_security_daily_payload(value)

    assert finalized["status"] == "attention"
    assert "存在 SSH 失败认证" in finalized["pending_confirmation"]
    assert any(item["title"] == "核查 SSH 失败认证" for item in finalized["actions"])


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        ("14 次", "9 次", "↑56%"),
        ("3", "3", "与昨日持平"),
        ("2 次", "0 次", "昨日 0"),
        ("0", "5", "↓100%"),
        ("不可用", "5", None),
    ],
)
def test_count_delta_suffix_formats_day_over_day(
    current: str,
    previous: str,
    expected: str | None,
) -> None:
    suffix = _count_delta_suffix(current, previous)
    if expected is None:
        assert suffix is None
    else:
        assert suffix is not None
        assert expected in suffix


def test_enrich_day_over_day_appends_comparison_to_metrics_and_summary() -> None:
    current = payload()
    previous = payload()
    current["metrics"][0]["value"] = "14 次"
    previous["metrics"][0]["value"] = "9 次"
    current["metrics"][3]["value"] = "3"
    previous["metrics"][3]["value"] = "1"
    current["web"][3]["value"] = "2 次命中"
    previous["web"][3]["value"] = "0 次命中"

    enriched = _enrich_day_over_day(current, previous)

    assert "（昨日 9，↑56%）" in enriched["metrics"][0]["value"]
    assert "（昨日 1，↑200%）" in enriched["metrics"][3]["value"]
    assert "（昨日 0，新增 2）" in enriched["web"][3]["value"]
    assert "较昨日：" in enriched["summary"]


@pytest.mark.asyncio
async def test_generate_latest_forces_manual_regeneration_and_sends(tmp_path: Path) -> None:
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(
        repository,
        control,
        control_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, 9, tzinfo=SHANGHAI),
    )
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    incoming.joinpath("2026-07-15.json").write_text(
        json.dumps(payload()), encoding="utf-8"
    )
    principal = SecurityPrincipal(1, 2, "admin", "security", "admin")

    result = await service.generate_latest(principal=principal, ip="127.0.0.1")

    assert result.generation_source == "manual"
    assert repository.ingested[-1]["force"] is True
    assert repository.ingested[-1]["generation_source"] == "manual"
    assert repository.ingested[-1]["payload"]["generated_at"] == (
        "2026-07-16T09:00:00+08:00"
    )
    assert len(control.submitted) == 1


@pytest.mark.asyncio
async def test_generate_latest_records_unavailable_manual_report_and_sends_problem(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(
        repository,
        control,
        control_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, 9, tzinfo=SHANGHAI),
    )
    # 不写入 incoming 快照：手动路径应新增一条 unavailable 记录并发送问题通报。

    result = await service.generate_latest(
        principal=SecurityPrincipal(1, 2, "admin", "security", "admin"),
        ip="127.0.0.1",
    )

    assert result.generation_source == "manual"
    assert result.generation_status == "unavailable"
    assert result.payload is None
    assert len(control.submitted) == 1
    body = control.submitted[0][1]
    validated = validate_security_daily_payload(body)
    assert "证据不可用" in validated["summary"]
    assert all(item["value"] == "不可用" for item in validated["metrics"][:4])


@pytest.mark.asyncio
async def test_generate_latest_refuses_when_mail_config_incomplete(tmp_path: Path) -> None:
    repository = FakeRepository(record())
    repository.config = SecurityDailyConfiguration(
        enabled=True,
        api_key="",
        recipients=(),
    )
    service = SecurityDailyService(
        repository,
        FakeControl(),
        control_dir=tmp_path,
        clock=lambda: datetime(2026, 7, 16, 9, tzinfo=SHANGHAI),
    )

    with pytest.raises(SecurityDailyUnavailable, match="发信配置不完整"):
        await service.generate_latest(
            principal=SecurityPrincipal(1, 2, "admin", "security", "admin"),
            ip="127.0.0.1",
        )

    assert repository.ingested == []


@pytest.mark.asyncio
async def test_submit_auto_delivery_sends_once_and_skips_pending() -> None:
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(repository, control)

    first = await service.submit_auto_delivery(date(2026, 7, 15))
    second = await service.submit_auto_delivery(date(2026, 7, 15))

    assert first is not None
    assert second is None
    assert len(control.submitted) == 1


@pytest.mark.asyncio
async def test_submit_auto_delivery_sends_problem_email_when_evidence_unavailable() -> None:
    unavailable = replace(
        record(),
        generation_status="unavailable",
        payload=None,
        last_error="安全日报证据源不可用",
    )
    repository = FakeRepository(unavailable)
    control = FakeControl()
    service = SecurityDailyService(repository, control)

    await service.submit_auto_delivery(date(2026, 7, 15))

    assert len(control.submitted) == 1
    body = control.submitted[0][1]
    validated = validate_security_daily_payload(body)
    assert "证据不可用" in validated["summary"]
    assert all(item["value"] == "不可用" for item in validated["metrics"][:4])


@pytest.mark.asyncio
async def test_submit_auto_delivery_marks_config_incomplete_instead_of_silent_skip() -> None:
    repository = FakeRepository(record())
    repository.config = SecurityDailyConfiguration(
        enabled=True,
        api_key="",
        recipients=(),
    )
    control = FakeControl()
    service = SecurityDailyService(repository, control)

    await service.submit_auto_delivery(date(2026, 7, 15))

    assert repository.delivery_failed == [
        (1, "安全日报发信配置不完整（缺少 Resend Key 或收件人）")
    ]
    assert control.submitted == []


@pytest.mark.asyncio
async def test_submit_auto_delivery_skips_already_sent_report() -> None:
    repository = FakeRepository(replace(record(), delivery_status="sent"))
    service = SecurityDailyService(repository, FakeControl())

    assert await service.submit_auto_delivery(date(2026, 7, 15)) is None


@pytest.mark.asyncio
async def test_submit_auto_delivery_skips_stale_payload_when_snapshot_is_newer(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    snapshot = incoming / "2026-07-15.json"
    snapshot.write_text(json.dumps(payload()), encoding="utf-8")
    fresh = datetime.now(SHANGHAI)

    os.utime(snapshot, (fresh.timestamp() + 3600, fresh.timestamp() + 3600))
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(repository, control, control_dir=tmp_path)

    assert await service.submit_auto_delivery(date(2026, 7, 15)) is None
    assert control.submitted == []


@pytest.mark.asyncio
async def test_submit_auto_delivery_sends_when_snapshot_predates_generation(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    snapshot = incoming / "2026-07-15.json"
    snapshot.write_text(json.dumps(payload()), encoding="utf-8")
    # 快照写入时间早于记录生成时间：正常自动路径必须发送，不能被误判为过期。
    os.utime(
        snapshot,
        (datetime(2026, 7, 16, 7, tzinfo=SHANGHAI).timestamp(),) * 2,
    )
    repository = FakeRepository(record())
    control = FakeControl()
    service = SecurityDailyService(repository, control, control_dir=tmp_path)

    request = await service.submit_auto_delivery(date(2026, 7, 15))

    assert request is not None
    assert len(control.submitted) == 1
