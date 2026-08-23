from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.crypto import CryptoService, EncryptionContext
from app.services.raw_replay import (
    RawIntegrityConflict,
    RawReplayConflict,
    RawReplayRecord,
    RawReplayService,
)
from app.services.report_ingest import ReportApplyResult, ReportIngestService

ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")


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
        self.has_audit = True

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

    async def has_human_raw_replay_audit(self, raw_id: int) -> bool:
        return self.has_audit

    async def audit_raw_replay(
        self,
        raw_id: int,
        *,
        source: str,
        items: int,
        actor: str,
        ip: str,
        system_producer: bool = False,
        principal: SecurityPrincipal | None = None,
    ) -> None:
        self.audits.append(
            {
                "raw_id": raw_id,
                "source": source,
                "items": items,
                "actor": actor,
                "ip": ip,
                "system_producer": system_producer,
                "account_id": None if principal is None else principal.account_id,
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

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
        return custom_ids

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
    ) -> None:
        self.events.append(("metadata", (raw_id, tuple(custom_ids), item_count)))

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

    count = await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert count == 1 and events[0][0] == source
    assert repository.audits == [
        {
            "raw_id": 9,
            "source": source,
            "items": 1,
            "actor": "admin01",
            "ip": "10.0.0.8",
            "system_producer": False,
            "account_id": 1,
        }
    ]
    assert "payload" not in str(repository.audits).lower()


@pytest.mark.asyncio
async def test_system_replay_requests_system_audit_producer() -> None:
    """reconcile 自动重放没有认证会话，审计必须显式声明 system 生产者。"""

    raw = payload([{"customId": "safe-custom-id"}])
    repository = FakeRepository(
        RawReplayRecord(9, "reply", b"ciphertext", hashlib.sha256(raw).hexdigest(), 2, False)
    )
    service = RawReplayService(
        repository,
        FakeCrypto(raw),
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    await service.replay(9, actor="system-reconcile", ip="127.0.0.1", system_producer=True)

    assert repository.audits == [
        {
            "raw_id": 9,
            "source": "reply",
            "items": 1,
            "actor": "system-reconcile",
            "ip": "127.0.0.1",
            "system_producer": True,
            "account_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_raw_replay_processes_vendor_local_report_time_without_refetching() -> None:
    raw = payload(
        [
            {
                "taskId": "task-1",
                "customId": "custom1",
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

    assert await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN) == 1

    # 重放路径重建 custom_ids 索引元数据（#341），再应用回执。
    assert [event[0] for event in report_repository.events] == [
        "metadata",
        "apply",
        "processed",
    ]
    assert report_repository.events[0][1] == (9, ("custom1",), 1)
    parsed = report_repository.events[1][1]
    assert parsed.report_time.isoformat() == "2026-07-21T16:41:51+08:00"
    assert replay_repository.audits == [
        {
            "raw_id": 9,
            "source": "report",
            "items": 1,
            "actor": "admin01",
            "ip": "10.0.0.8",
            "system_producer": False,
            "account_id": 1,
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
        await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

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
        await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

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
        await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert events == []
    assert repository.errors == [(9, "raw payload integrity mismatch")]


@pytest.mark.asyncio
async def test_unsupported_wire_encoding_remains_rejected_during_replay() -> None:
    raw = b"compressed-wire-bytes"
    repository = FakeRepository(
        RawReplayRecord(
            9,
            "report",
            b"ciphertext",
            hashlib.sha256(raw).hexdigest(),
            1,
            False,
            200,
            "unsupported",
        )
    )
    service = RawReplayService(
        repository,
        FakeCrypto(raw),
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    with pytest.raises(RawIntegrityConflict, match="envelope"):
        await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert repository.errors == [(9, "raw vendor envelope is invalid")]


@pytest.mark.asyncio
async def test_human_replay_fails_closed_before_claim_when_actor_mismatches() -> None:
    repository = FakeRepository(
        RawReplayRecord(9, "report", b"x", "0" * 64, 1, False)
    )
    service = RawReplayService(
        repository,
        FakeCrypto(b""),
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    with pytest.raises(RuntimeError, match="audit principal"):
        await service.replay(9, actor="other-admin", ip="10.0.0.8", principal=ADMIN)

    assert repository.claim_calls == []
    assert repository.audits == []


@pytest.mark.asyncio
async def test_system_replay_rejects_human_principal_before_claim() -> None:
    repository = FakeRepository(
        RawReplayRecord(9, "reply", b"x", "0" * 64, 1, False)
    )
    service = RawReplayService(
        repository,
        FakeCrypto(b""),
        FakeProcessor("report", []),
        FakeProcessor("reply", []),
    )

    with pytest.raises(RuntimeError, match="cannot bind a human"):
        await service.replay(
            9,
            actor="system-reconcile",
            ip="127.0.0.1",
            system_producer=True,
            principal=ADMIN,
        )

    assert repository.claim_calls == []


@pytest.mark.asyncio
async def test_processed_raw_retries_missing_human_audit_without_reprocessing() -> None:
    raw = payload([{"customId": "safe-custom-id"}])
    repository = FakeRepository(
        RawReplayRecord(
            9,
            "report",
            b"ciphertext",
            hashlib.sha256(raw).hexdigest(),
            2,
            True,
            item_count=3,
        ),
        claimed=False,
    )
    repository.has_audit = False
    events: list[tuple[str, object]] = []
    service = RawReplayService(
        repository,
        FakeCrypto(raw),
        FakeProcessor("report", events),
        FakeProcessor("reply", events),
    )

    count = await service.replay(9, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert count == 3
    assert events == []
    assert repository.audits == [
        {
            "raw_id": 9,
            "source": "report",
            "items": 3,
            "actor": "admin01",
            "ip": "10.0.0.8",
            "system_producer": False,
            "account_id": 1,
        }
    ]
