from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services.crypto import CryptoService, EncryptionContext
from app.services.uncertain import RawCandidate, UncertainChunk, UncertainReconciler


def crypto() -> CryptoService:
    key = base64.b64encode(b"u" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self, chunks: list[UncertainChunk], candidates: list[RawCandidate]) -> None:
        self.chunks = chunks
        self.candidates = candidates
        self.resolved: list[tuple[int, str]] = []
        self.alerts: list[int] = []

    async def list_uncertain(self) -> list[UncertainChunk]:
        return self.chunks

    async def raw_candidates(self, custom_id: str) -> list[RawCandidate]:
        return self.candidates

    async def resolve_submitted(self, chunk_id: int, task_id: str) -> None:
        self.resolved.append((chunk_id, task_id))

    async def alert_overdue(self, chunk: UncertainChunk) -> None:
        self.alerts.append(chunk.chunk_id)


def raw(service: CryptoService, custom_id: str, *, task_id: str = "task-9") -> RawCandidate:
    payload = json.dumps(
        {
            "code": 0,
            "data": [{"customId": custom_id, "taskId": task_id, "phone": "13800138000"}],
        }
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    encrypted = service.encrypt_bound_bytes(
        payload,
        EncryptionContext(
            domain="vendor-raw",
            table="raw_vendor_log",
            column="payload_enc",
            object_id=f"report:{digest}",
        ),
    )
    return RawCandidate(1, "report", encrypted.payload, digest, encrypted.key_version)


@pytest.mark.asyncio
async def test_reconcile_only_transitions_after_decrypted_custom_id_confirmation() -> None:
    service = crypto()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    chunk = UncertainChunk(3, "custom-1", now - timedelta(minutes=5))
    repository = FakeRepository([chunk], [raw(service, "custom-1")])
    assert await UncertainReconciler(repository, service, clock=lambda: now).run_once() == 1
    assert len(repository.resolved) == 1
    resolved_chunk, resolved_task = repository.resolved[0]
    assert resolved_chunk == 3
    assert len(resolved_task) == 64
    assert "task-9" not in resolved_task
    assert repository.alerts == []


@pytest.mark.asyncio
async def test_mismatched_raw_never_resolves_and_overdue_only_alerts() -> None:
    service = crypto()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    chunk = UncertainChunk(4, "custom-missing", now - timedelta(hours=25))
    repository = FakeRepository([chunk], [raw(service, "another-custom")])
    assert await UncertainReconciler(repository, service, clock=lambda: now).run_once() == 0
    assert repository.resolved == []
    assert repository.alerts == [4]


@pytest.mark.asyncio
async def test_malformed_task_id_does_not_crash_reconcile_run() -> None:
    service = crypto()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    overdue = UncertainChunk(6, "custom-1", now - timedelta(hours=25))
    repository = FakeRepository(
        [overdue],
        [raw(service, "custom-1", task_id="bad task:id!")],
    )

    assert await UncertainReconciler(repository, service, clock=lambda: now).run_once() == 0

    assert repository.resolved == []
    assert repository.alerts == [6]


@pytest.mark.asyncio
async def test_malformed_task_id_candidate_is_skipped_for_valid_one() -> None:
    service = crypto()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    chunk = UncertainChunk(7, "custom-1", now - timedelta(minutes=5))
    repository = FakeRepository(
        [chunk],
        [
            raw(service, "custom-1", task_id="bad task:id!"),
            raw(service, "custom-1", task_id="good9"),
        ],
    )

    assert await UncertainReconciler(repository, service, clock=lambda: now).run_once() == 1

    assert len(repository.resolved) == 1
    assert repository.resolved[0][0] == 7


@pytest.mark.asyncio
async def test_uncertain_threshold_comes_from_runtime_policy() -> None:
    from app.services.runtime_policy import RuntimePolicy

    service = crypto()
    now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    repository = FakeRepository(
        [UncertainChunk(5, "missing", now - timedelta(hours=2))],
        [],
    )

    reconciler = UncertainReconciler.from_policy(
        repository,
        service,
        RuntimePolicy.from_mapping({"uncertain_alert_hours": "1"}),
        clock=lambda: now,
    )
    await reconciler.run_once()

    assert repository.alerts == [5]
