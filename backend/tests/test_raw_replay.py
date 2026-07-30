from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from app.services.crypto import CryptoService, EncryptionContext
from app.services.raw_replay import (
    RawIntegrityConflict,
    RawReplayConflict,
    RawReplayRecord,
    RawReplayService,
)
from app.services.report_ingest import ReportApplyResult, ReportIngestService


def payload(data: list[dict[str, object]]) -> bytes:
    return json.dumps({"code": 0, "msg": "ok", "data": data}).encode()


class FakeCrypto:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[tuple[bytes, int]] = []

    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = True,
    ) -> bytes:
        self.calls.append((payload, key_version))
        return self.raw


class FakeRepository:
    def __init__(
        self,
        record: RawReplayRecord | None,
        *,
        claimed: bool | None = None,
    ) -> None:
        self.record = record
        self.claimed = (
            claimed
            if claimed is not None
            else record is not None and not record.processed
        )
        self.claim_calls: list[int] = []
        self.errors: list[tuple[int, str]] = []
        self.audits: list[dict[str, object]] = []

    async def get_raw_for_replay(self, raw_id: int) -> RawReplayRecord | None:
        return self.record

    async def claim_raw_for_replay(self, raw_id: int) -> object:
        self.claim_calls.append(raw_id)
        if self.record is None:
            return None
        return type(
            "Claim",
            (),
            {"record": self.record, "claimed": self.claimed},
        )()

    async def mark_replay_error(self, raw_id: int, error: str) -> None:
        self.errors.append((raw_id, error))

    async def audit_raw_replay(
        self, raw_id: int, *, source: str, items: int, actor: str, ip: str
    ) -> None:
        self.audits.append(
            {
                "raw_id": raw_id,
                "source": source,
                "items": items,
                "actor": actor,
                "ip": ip,
            }
        )


class FakeProcessor:
    def __init__(self, label: str, events: list[tuple[str, object]]) -> None:
        self.label = label
        self.events = events

    async def process_existing(self, raw_id: int, data: object) -> int:
        self.events.append((self.label, (raw_id, data)))
        return len(data) if isinstance(data, list) else 0


class FakeReportRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        raise AssertionError("raw replay must not persist or refetch a report")

    async def apply_report(self, raw_id: int, report: object) -> ReportApplyResult:
        self.events.append(("apply", report))
        return ReportApplyResult(8, True)

    async def failure_rate_candidate(self, batch_id: int) -> None:
        return None

    async def persist_unmatched(self, raw_id: int, report: object) -> None:
        self.events.append(("unmatched", report))

    async def mark_processed(self, raw_id: int) -> None:
        self.events.append(("processed", raw_id))

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.events.append(("error", error))


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["report", "reply"])
async def test_raw_replay_verifies_hash_routes_existing_payload_and_audits_metadata(
    source: str,
) -> None:
    raw = payload([{"customId": "safe-custom-id"}])
    repository = FakeRepository(
        RawReplayRecord(9, source, b"ciphertext", hashlib.sha256(raw).hexdigest(), 2, False)
    )
    events: list[tuple[str, object]] = []
    service = RawReplayService(
        repository,
        FakeCrypto(raw),
        FakeProcessor("report", events),
        FakeProcessor("reply", events),
    )

    count = await service.replay(9, actor="admin01", ip="10.0.0.8")

    assert count == 1 and events[0][0] == source
    assert repository.audits == [
        {
            "raw_id": 9,
            "source": source,
            "items": 1,
            "actor": "admin01",
            "ip": "10.0.0.8",
        }
    ]
    assert "payload" not in str(repository.audits).lower()


@pytest.mark.asyncio
async def test_raw_replay_processes_vendor_local_report_time_without_refetching() -> None:
    raw = payload(
        [
            {
                "taskId": "task-1",
                "customId": "custom-1",
                "phone": "13800138000",
                "reportStatus": 1,
                "reportDescription": "DELIVRD",
                "reportTime": "2026-07-21 16:41:51",
            }
        ]
    )
    replay_repository = FakeRepository(
        RawReplayRecord(9, "report", b"ciphertext", hashlib.sha256(raw).hexdigest(), 2, False)
    )
    report_repository = FakeReportRepository()
    key = base64.b64encode(b"r" * 32).decode()
    report_processor = ReportIngestService(
        None,
        report_repository,
        CryptoService.from_secret_values(key, key),
    )
    service = RawReplayService(
        replay_repository,
        FakeCrypto(raw),
        report_processor,
        FakeProcessor("reply", []),
    )

    assert await service.replay(9, actor="admin01", ip="10.0.0.8") == 1

    assert [event[0] for event in report_repository.events] == ["apply", "processed"]
    parsed = report_repository.events[0][1]
    assert parsed.report_time.isoformat() == "2026-07-21T16:41:51+08:00"
    assert replay_repository.audits == [
        {
            "raw_id": 9,
            "source": "report",
            "items": 1,
            "actor": "admin01",
            "ip": "10.0.0.8",
        }
    ]


@pytest.mark.asyncio
async def test_processed_raw_is_rejected_before_decryption() -> None:
    raw = payload([])
    crypto = FakeCrypto(raw)
    service = RawReplayService(
        FakeRepository(
            RawReplayRecord(9, "report", b"x", hashlib.sha256(raw).hexdigest(), 1, True)
        ),
        crypto,
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    with pytest.raises(RawReplayConflict):
        await service.replay(9, actor="admin01", ip="10.0.0.8")

    assert crypto.calls == []


@pytest.mark.asyncio
async def test_concurrent_raw_replay_is_rejected_before_decryption() -> None:
    raw = payload([])
    crypto = FakeCrypto(raw)
    repository = FakeRepository(
        RawReplayRecord(9, "report", b"x", hashlib.sha256(raw).hexdigest(), 1, False),
        claimed=False,
    )
    service = RawReplayService(
        repository,
        crypto,
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    with pytest.raises(RawReplayConflict, match="处理中"):
        await service.replay(9, actor="admin01", ip="10.0.0.8")

    assert repository.claim_calls == [9]
    assert crypto.calls == []


@pytest.mark.asyncio
async def test_integrity_failure_keeps_raw_unprocessed_and_records_safe_error() -> None:
    raw = payload([])
    repository = FakeRepository(RawReplayRecord(9, "report", b"x", "0" * 64, 1, False))
    events: list[tuple[str, object]] = []
    service = RawReplayService(
        repository,
        FakeCrypto(raw),
        FakeProcessor("report", events),
        FakeProcessor("reply", events),
    )

    with pytest.raises(RawIntegrityConflict):
        await service.replay(9, actor="admin01", ip="10.0.0.8")

    assert events == []
    assert repository.errors == [(9, "raw payload integrity mismatch")]
