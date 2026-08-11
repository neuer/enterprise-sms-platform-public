from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest

from app.services.crypto import CryptoService
from app.services.report_ingest import (
    FailureRateAlert,
    ReportApplyResult,
    ReportIngestService,
)
from app.services.report_repository import evaluate_failure_rate
from app.vendor.zhihui import RawPulledPayload


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def rotated_crypto() -> CryptoService:
    first = base64.b64encode(b"r" * 32).decode()
    second = base64.b64encode(b"s" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    return CryptoService.from_secret_values(ring, ring)


class FakeGateway:
    def __init__(self, records: Any) -> None:
        raw = json.dumps({"code": 0, "msg": None, "data": records}).encode()
        self.result = RawPulledPayload(raw, 200)

    async def get_report_raw(self) -> RawPulledPayload:
        return self.result


class FakeRepository:
    def __init__(self, *, matched: bool = True, changed: bool = True) -> None:
        self.matched = matched
        self.changed = changed
        self.events: list[tuple[str, Any]] = []
        self.failure_rate: FailureRateAlert | None = None

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(("persist_raw", values))
        return 12

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
    ) -> None:
        self.events.append(("metadata", (raw_id, custom_ids, item_count)))

    async def apply_report(self, raw_id: int, report: Any) -> ReportApplyResult | None:
        self.events.append(("apply", report))
        return ReportApplyResult(8, self.changed) if self.matched else None

    async def failure_rate_candidate(self, batch_id: int) -> FailureRateAlert | None:
        self.events.append(("failure_rate", batch_id))
        return self.failure_rate

    async def persist_unmatched(self, raw_id: int, report: Any) -> None:
        self.events.append(("unmatched", report))

    async def mark_processed(self, raw_id: int) -> None:
        self.events.append(("processed", raw_id))

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.events.append(("error", error))


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def report() -> dict[str, Any]:
    return {
        "taskId": "task-1",
        "customId": "custom-1",
        "phone": "13800138000",
        "reportStatus": 1,
        "reportDescription": "DELIVRD",
        "reportTime": "2026-07-11T08:00:00Z",
    }


@pytest.mark.asyncio
async def test_raw_response_is_encrypted_and_committed_before_parsing() -> None:
    repository = FakeRepository()
    service = ReportIngestService(FakeGateway([report()]), repository, crypto())
    assert await service.poll_once() == 1

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "apply",
        "processed",
    ]
    raw_values = repository.events[0][1]
    assert b"13800138000" not in raw_values["payload_enc"]
    assert raw_values["custom_ids"] == []
    assert raw_values["item_count"] == 0
    assert raw_values["http_status"] == 200
    assert raw_values["content_encoding"] == "identity"
    assert repository.events[1][1] == (12, ["custom-1"], 1)
    parsed = repository.events[2][1]
    assert not hasattr(parsed, "phone")
    assert parsed.phone_mask == "138****8000"


@pytest.mark.asyncio
async def test_report_parser_tolerates_vendor_custom_id_key_typo() -> None:
    item = report()
    item["customId "] = item.pop("customId")
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (12, ["custom-1"], 1)
    assert repository.events[2][1].custom_id == "custom-1"


@pytest.mark.asyncio
async def test_report_description_masks_embedded_phone_before_projection_persistence() -> None:
    item = report() | {"reportDescription": "号码13800138000投递失败"}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    parsed = repository.events[2][1]
    assert parsed.report_desc == "号码138****8000投递失败"
    assert "13800138000" not in parsed.report_desc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-21 16:41:51", "2026-07-21T16:41:51+08:00"),
        ("2021-05-12T06:45:17.529Z", "2021-05-12T06:45:17.529000+00:00"),
        ("2026-07-21T09:41:51+01:00", "2026-07-21T09:41:51+01:00"),
    ],
)
async def test_vendor_report_time_preserves_explicit_zone_and_defaults_to_shanghai(
    value: str,
    expected: str,
) -> None:
    item = report() | {"reportTime": value}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    parsed = repository.events[2][1]
    assert parsed.report_time.isoformat() == expected


@pytest.mark.asyncio
async def test_invalid_report_time_is_not_copied_into_safe_raw_error() -> None:
    sensitive_value = "not-a-date 13800138000"
    repository = FakeRepository()

    with pytest.raises(ValueError, match="reportTime format is invalid"):
        await ReportIngestService(
            FakeGateway([report() | {"reportTime": sensitive_value}]),
            repository,
            crypto(),
        ).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "metadata", "error"]
    error = repository.events[-1][1]
    assert sensitive_value not in error
    assert "13800138000" not in error


@pytest.mark.asyncio
async def test_report_keeps_all_hmac_versions_in_memory_with_active_primary() -> None:
    service_crypto = rotated_crypto()
    repository = FakeRepository()

    await ReportIngestService(
        FakeGateway([report()]),
        repository,
        service_crypto,
    ).poll_once()

    parsed = repository.events[2][1]
    candidates = service_crypto.hmac_candidates("13800138000")
    assert parsed.phone_hmac == candidates[2]
    assert parsed.phone_hmacs == tuple(candidates.values())
    assert parsed.phone_hmac in parsed.phone_hmacs


@pytest.mark.asyncio
async def test_report_event_key_is_stable_when_active_hmac_version_rotates() -> None:
    before = FakeRepository()
    after = FakeRepository()

    await ReportIngestService(FakeGateway([report()]), before, crypto()).poll_once()
    await ReportIngestService(
        FakeGateway([report()]),
        after,
        rotated_crypto(),
    ).poll_once()

    assert before.events[2][1].event_key == after.events[2][1].event_key
    assert before.events[2][1].phone_hmac != after.events[2][1].phone_hmac


@pytest.mark.asyncio
async def test_unmatched_report_is_preserved_with_phone_protection() -> None:
    repository = FakeRepository(matched=False)
    await ReportIngestService(FakeGateway([report()]), repository, crypto()).poll_once()
    unmatched = next(value for event, value in repository.events if event == "unmatched")
    assert unmatched.phone_mask == "138****8000"
    assert b"13800138000" not in unmatched.phone_enc


@pytest.mark.asyncio
async def test_parse_failure_keeps_raw_unprocessed_for_replay() -> None:
    broken = report() | {"phone": "invalid"}
    repository = FakeRepository()
    with pytest.raises(ValueError):
        await ReportIngestService(FakeGateway([broken]), repository, crypto()).poll_once()
    assert [event[0] for event in repository.events] == ["persist_raw", "metadata", "error"]


@pytest.mark.asyncio
async def test_invalid_data_shape_is_still_persisted_before_error() -> None:
    repository = FakeRepository()
    with pytest.raises(ValueError, match="object array"):
        await ReportIngestService(FakeGateway("broken"), repository, crypto()).poll_once()
    assert [event[0] for event in repository.events] == ["persist_raw", "error"]


@pytest.mark.asyncio
async def test_non_json_pull_is_persisted_before_protocol_error() -> None:
    repository = FakeRepository()
    gateway = FakeGateway([])
    gateway.result = RawPulledPayload(b"not-json", 200)

    with pytest.raises(Exception, match="not JSON"):
        await ReportIngestService(gateway, repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "error"]


@pytest.mark.asyncio
async def test_compressed_wire_response_is_persisted_before_rejection() -> None:
    repository = FakeRepository()
    gateway = FakeGateway([])
    wire_bytes = gzip.compress(b"x" * (8 * 1024 * 1024))
    gateway.result = RawPulledPayload(wire_bytes, 200, "unsupported")

    with pytest.raises(Exception, match="content-encoding"):
        await ReportIngestService(gateway, repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "error"]
    persisted = repository.events[0][1]
    assert persisted["content_encoding"] == "unsupported"
    assert persisted["item_count"] == 0 and persisted["custom_ids"] == []


def test_failure_rate_requires_terminal_batch_minimum_and_strict_threshold() -> None:
    common = {
        "batch_id": 8,
        "batch_no": "BATCH-8",
        "status": "completed",
        "threshold": 20,
        "min_total": 50,
    }
    assert evaluate_failure_rate(common | {"delivered": 39, "failed": 10}) is None
    assert evaluate_failure_rate(common | {"delivered": 40, "failed": 10}) is None
    assert evaluate_failure_rate(common | {"delivered": 79, "failed": 21}) == FailureRateAlert(
        batch_id=8,
        batch_no="BATCH-8",
        delivered=79,
        failed=21,
        threshold=20,
    )
    assert (
        evaluate_failure_rate(common | {"status": "sending", "delivered": 79, "failed": 21}) is None
    )


@pytest.mark.asyncio
async def test_terminal_failure_rate_candidate_emits_batch_scoped_alert() -> None:
    repository = FakeRepository()
    repository.failure_rate = FailureRateAlert(8, "BATCH-8", 79, 21, 20)
    alerts = FakeAlerts()

    await ReportIngestService(
        FakeGateway([report()]),
        repository,
        crypto(),
        alerts=alerts,
    ).poll_once()

    assert alerts.events == [
        {
            "alert_type": "failure_rate",
            "level": "warn",
            "title": "批次失败率超过阈值",
            "detail": {
                "batch_no": "BATCH-8",
                "delivered": 79,
                "failed": 21,
                "failure_rate_percent": 21.0,
                "threshold_percent": 20,
            },
            "dedup_key": "failure_rate:BATCH-8",
        }
    ]


@pytest.mark.asyncio
async def test_ignored_report_projection_does_not_recompute_failure_alert() -> None:
    repository = FakeRepository(changed=False)
    repository.failure_rate = FailureRateAlert(8, "BATCH-8", 79, 21, 20)
    alerts = FakeAlerts()

    await ReportIngestService(
        FakeGateway([report()]),
        repository,
        crypto(),
        alerts=alerts,
    ).poll_once()

    assert "failure_rate" not in [event for event, _value in repository.events]
    assert alerts.events == []
