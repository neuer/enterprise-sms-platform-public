from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest

import app.services.report_ingest as report_ingest_module
from app.services.crypto import CryptoService, EncryptionContext
from app.services.raw_spill import RawSpillStore
from app.services.report_ingest import (
    FailureRateAlert,
    ReportApplyResult,
    ReportIngestService,
)
from app.services.report_repository import evaluate_failure_rate
from app.vendor.zhihui import RawPulledPayload, VendorResponseTooLarge


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

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        if body_sink is not None:
            announce = getattr(body_sink, "announce", None)
            if callable(announce):
                announce(
                    http_status=self.result.status_code,
                    content_encoding=self.result.content_encoding,
                    protocol_invalid=self.result.protocol_invalid,
                )
            if self.result.raw_payload:
                body_sink.feed(self.result.raw_payload)
            finish = getattr(body_sink, "finish", None)
            if callable(finish):
                finish(
                    complete=True,
                    http_status=self.result.status_code,
                    content_encoding=self.result.content_encoding,
                    protocol_invalid=self.result.protocol_invalid,
                )
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
        **_: object,
    ) -> None:
        self.events.append(("metadata", (raw_id, custom_ids, item_count)))

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
        return [value for value in custom_ids if value == "custom1"]

    async def apply_report(self, raw_id: int, report: Any) -> ReportApplyResult | None:
        self.events.append(("apply", report))
        return ReportApplyResult(8, self.changed) if self.matched else None

    async def failure_rate_candidate(self, batch_id: int) -> FailureRateAlert | None:
        self.events.append(("failure_rate", batch_id))
        return self.failure_rate

    async def persist_unmatched(self, raw_id: int, report: Any) -> None:
        self.events.append(("unmatched", report))

    async def mark_processed(self, raw_id: int, **_: object) -> None:
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
        "customId": "custom1",
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
    assert repository.events[1][1] == (12, ["custom1"], 1)
    parsed = repository.events[2][1]
    assert not hasattr(parsed, "phone")
    assert parsed.phone_mask == "138****8000"
    assert parsed.match_custom_id == "custom1"
    assert len(parsed.custom_id) == 64
    assert len(parsed.vendor_task_id) == 64
    assert "task-1" not in parsed.vendor_task_id


@pytest.mark.asyncio
async def test_report_parser_tolerates_vendor_custom_id_key_typo() -> None:
    item = report()
    item["customId "] = item.pop("customId")
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (12, ["custom1"], 1)
    assert repository.events[2][1].match_custom_id == "custom1"
    assert len(repository.events[2][1].custom_id) == 64


@pytest.mark.asyncio
async def test_phone_like_task_id_degrades_to_pseudonym_and_still_applies() -> None:
    item = report() | {"taskId": "13800138000"}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    applied = [value for event, value in repository.events if event == "apply"]
    assert len(applied) == 1
    assert applied[0].match_custom_id == "custom1"
    assert len(applied[0].vendor_task_id) == 64
    assert "13800138000" not in applied[0].vendor_task_id
    assert ("processed", 12) in repository.events
    assert not any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_phone_like_custom_id_is_preserved_as_unmatched_pseudonym() -> None:
    item = report() | {"customId": "13800138000"}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert not any(event[0] == "apply" for event in repository.events)
    unmatched = [value for event, value in repository.events if event == "unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0].match_custom_id == ""
    assert len(unmatched[0].custom_id) == 64
    assert "13800138000" not in unmatched[0].custom_id
    assert ("processed", 12) in repository.events
    assert not any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_empty_custom_id_report_is_preserved_as_unmatched() -> None:
    item = report() | {"customId": ""}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert not any(event[0] == "apply" for event in repository.events)
    unmatched = [value for event, value in repository.events if event == "unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0].match_custom_id == ""
    assert len(unmatched[0].custom_id) == 64
    assert ("processed", 12) in repository.events
    assert not any(event[0] == "error" for event in repository.events)


class OversizedGateway:
    def __init__(self, error: VendorResponseTooLarge) -> None:
        self.error = error

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        if body_sink is not None and self.error.raw_body:
            body_sink.feed(self.error.raw_body)
            finish = getattr(body_sink, "finish", None)
            if callable(finish):
                finish(complete=self.error.complete, too_large=self.error.complete)
        raise self.error


@pytest.mark.asyncio
async def test_oversized_report_fallback_persists_valid_http_status() -> None:
    repository = FakeRepository()
    gateway = OversizedGateway(
        VendorResponseTooLarge("too large", raw_body=b'{"code":0,"data":['),
    )

    with pytest.raises(VendorResponseTooLarge):
        await ReportIngestService(gateway, repository, crypto()).poll_once()

    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert len(persisted) == 1
    assert persisted[0]["http_status"] == 200
    assert any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_oversized_report_fallback_keeps_vendor_http_status() -> None:
    repository = FakeRepository()
    gateway = OversizedGateway(
        VendorResponseTooLarge("too large", raw_body=b"partial", status_code=502),
    )

    with pytest.raises(VendorResponseTooLarge):
        await ReportIngestService(gateway, repository, crypto()).poll_once()

    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["http_status"] == 502


@pytest.mark.asyncio
async def test_platform_hex_custom_id_is_not_rejected_as_phone() -> None:
    custom_id = "390d6892939546adb08dc16600000001"
    item = report() | {"customId": custom_id}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    applied = [value for event, value in repository.events if event == "apply"]
    assert len(applied) == 1
    assert applied[0].match_custom_id == custom_id


@pytest.mark.asyncio
async def test_invalid_custom_id_does_not_abort_valid_report_items() -> None:
    repository = FakeRepository()
    mixed = [report() | {"customId": "legacy-x"}, report()]

    await ReportIngestService(FakeGateway(mixed), repository, crypto()).poll_once()

    assert repository.events[1][1] == (12, ["custom1"], 2)
    applied = [value for event, value in repository.events if event == "apply"]
    assert len(applied) == 1
    assert applied[0].match_custom_id == "custom1"
    unmatched = [value for event, value in repository.events if event == "unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0].match_custom_id == ""
    assert "legacy-x" not in unmatched[0].custom_id
    assert not any(event[0] == "error" for event in repository.events)
    assert ("processed", 12) in repository.events


@pytest.mark.asyncio
async def test_untrusted_identifier_text_is_only_persisted_as_hmac_pseudonym() -> None:
    item = report() | {"taskId": "OTP123456", "customId": "SecretOTP123456"}
    repository = FakeRepository()

    await ReportIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (12, [], 1)
    parsed = repository.events[2][1]
    assert parsed.match_custom_id == "SecretOTP123456"
    assert len(parsed.vendor_task_id) == 64
    assert len(parsed.custom_id) == 64
    assert "OTP123456" not in parsed.vendor_task_id + parsed.custom_id


@pytest.mark.asyncio
async def test_identifier_pseudonymization_preserves_legacy_report_event_key() -> None:
    repository = FakeRepository()
    service = ReportIngestService(
        FakeGateway([report()]),
        repository,
        crypto(),
    )

    await service.poll_once()

    parsed = repository.events[2][1]
    expected = report_ingest_module._report_event_key(
        vendor_task_id="task-1",
        custom_id="custom1",
        canonical_phone_hmac=parsed.phone_hmac,
        report_status=1,
        report_desc="DELIVRD",
        report_time=parsed.report_time,
    )
    assert parsed.event_key == expected


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
async def test_invalid_report_time_is_skipped_without_leaking_phone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_value = "not-a-date 13800138000"
    repository = FakeRepository()

    await ReportIngestService(
        FakeGateway([report() | {"reportTime": sensitive_value}]),
        repository,
        crypto(),
    ).poll_once()

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "error",
    ]
    rendered = repr(repository.events) + caplog.text
    assert sensitive_value not in rendered
    assert "13800138000" not in rendered


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
async def test_parse_failure_skips_item_and_keeps_raw_replayable() -> None:
    broken = report() | {"phone": "invalid"}
    repository = FakeRepository()
    alerts = FakeAlerts()

    await ReportIngestService(
        FakeGateway([broken]),
        repository,
        crypto(),
        alerts=alerts,
    ).poll_once()

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "error",
    ]
    assert alerts.events
    assert alerts.events[0]["alert_type"] == "raw_item_skipped"
    assert alerts.events[0]["level"] == "crit"


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


@pytest.mark.asyncio
async def test_persist_failure_emits_crit_alert() -> None:
    class FailingRepository(FakeRepository):
        async def persist_raw(self, **values: Any) -> int:
            raise RuntimeError("database unavailable")

    alerts = FakeAlerts()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await ReportIngestService(
            FakeGateway([report()]),
            FailingRepository(),
            crypto(),
            alerts=alerts,
        ).poll_once()
    assert alerts.events
    assert alerts.events[0]["alert_type"] == "vendor_raw_persist_failed"
    assert alerts.events[0]["level"] == "crit"


@pytest.mark.asyncio
async def test_spill_survives_persist_gap_and_can_be_recovered(tmp_path: Any) -> None:
    import hashlib

    repository = FakeRepository()
    spill = RawSpillStore(tmp_path)
    service = ReportIngestService(
        FakeGateway([report()]),
        repository,
        crypto(),
        spill=spill,
    )
    pulled = await FakeGateway([report()]).get_report_raw()
    payload_sha256 = hashlib.sha256(pulled.raw_payload).hexdigest()
    encrypted = crypto().encrypt_bound_bytes(
        pulled.raw_payload,
        EncryptionContext(
            domain="vendor-raw",
            table="raw_vendor_log",
            column="payload_enc",
            object_id=f"report:{payload_sha256}",
        ),
    )
    spill.write(
        source="report",
        payload_sha256=payload_sha256,
        key_version=encrypted.key_version,
        http_status=200,
        content_encoding="identity",
        payload_enc=encrypted.payload,
        crypto=crypto(),
    )
    assert spill.list_pending()
    assert await service.recover_spills() == 1
    assert spill.list_pending() == []
    assert repository.events[0][0] == "persist_raw"


class _BrokenStream:
    def feed(self, chunk: bytes) -> bool:
        return True

    def finish(self, **_: Any) -> None:
        return None

    def discard(self) -> None:
        return None


class BrokenSpill:
    """磁盘满/权限错等 spill 故障；不得反向阻断 DB 落库。"""

    def write(self, **values: Any) -> None:
        raise OSError("no space left on device")

    def remove(self, source: str, payload_sha256: str) -> None:
        raise OSError("no space left on device")

    def list_pending(self) -> list[Any]:
        return []

    def list_pending_streams(self, crypto: Any) -> list[Any]:
        return []

    def can_accept(self, additional_bytes: int = 0) -> bool:
        return True

    def open_stream(self, source: str, crypto: Any) -> _BrokenStream:
        return _BrokenStream()


@pytest.mark.asyncio
async def test_spill_write_failure_degrades_to_alert_and_db_persist_continues() -> None:
    repository = FakeRepository()
    alerts = FakeAlerts()
    service = ReportIngestService(
        FakeGateway([report()]),
        repository,
        crypto(),
        alerts=alerts,
        spill=BrokenSpill(),  # type: ignore[arg-type]
    )

    assert await service.poll_once() == 1

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "apply",
        "failure_rate",
        "processed",
    ]
    spill_alerts = [
        event for event in alerts.events if event["alert_type"] == "vendor_raw_spill_failed"
    ]
    assert len(spill_alerts) == 1
    assert spill_alerts[0]["level"] == "crit"


@pytest.mark.asyncio
async def test_lease_heartbeat_loss_stops_large_item_writes_without_burning_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from uuid import UUID

    from app.services.raw_lease import RawLeaseLost, RawProcessingLease

    class LostBeat:
        def raise_if_lost(self) -> None:
            raise RawLeaseLost("heartbeat lost")

    @asynccontextmanager
    async def fake_bind(*_args: object, **_kwargs: object):
        yield LostBeat()

    monkeypatch.setattr(report_ingest_module, "bind_raw_lease_heartbeat", fake_bind)
    repository = FakeRepository()
    items = [report() for _ in range(64)]
    lease = RawProcessingLease(12, UUID(int=12), 1)
    with pytest.raises(RawLeaseLost, match="heartbeat lost"):
        await ReportIngestService(None, repository, crypto()).process_existing(
            12,
            items,
            lease=lease,
        )
    names = [event[0] for event in repository.events]
    assert "apply" not in names
    assert "processed" not in names
    assert "error" not in names
