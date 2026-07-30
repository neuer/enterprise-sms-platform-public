from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.services.crypto import CryptoService
from app.services.export import ExportFilterSet, ExportTooLarge
from app.services.export_file import ExportFileCodec
from app.services.export_repository import ExportClaim, ExportLeaseLost
from app.services.export_worker import ExportWorker

LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")


def crypto() -> CryptoService:
    key = base64.b64encode(b"w" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def claim(*, decrypted: bool) -> ExportClaim:
    return ExportClaim(
        9,
        ExportFilterSet(None, None, None, None, None, None, (), "平台部"),
        decrypted,
        LEASE_ID,
        datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
    )


def unmatched_claim(*, decrypted: bool) -> ExportClaim:
    return ExportClaim(
        10,
        ExportFilterSet(None, None, None, None, None, None, (), None, dataset="unmatched"),
        decrypted,
        LEASE_ID,
        datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
    )


class FakeRepository:
    def __init__(self, current: ExportClaim | None, values: list[dict[str, object]]) -> None:
        self.current = current
        self.values = values
        self.done: tuple[object, ...] | None = None
        self.failed: tuple[object, ...] | None = None

    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int,
    ) -> ExportClaim | None:
        assert lease_seconds >= 3
        return self.current

    async def heartbeat(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool:
        assert task_id in {9, 10}
        assert lease_id == LEASE_ID and lease_seconds >= 3
        return True

    async def rows(self, claim: ExportClaim) -> AsyncIterator[dict[str, object]]:
        for value in self.values:
            yield value

    async def mark_done(
        self,
        task_id: int,
        *,
        lease_id: UUID,
        file_path: str,
        row_count: int,
    ) -> None:
        self.done = (task_id, lease_id, file_path, row_count)

    async def mark_failed(self, task_id: int, *, lease_id: UUID) -> None:
        self.failed = (task_id, lease_id)


def row(*, decrypted: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 1,
        "created_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        "batch_no": "BATCH-1",
        "category": "notice",
        "status": "delivered",
        "report_desc": "DELIVRD",
        "report_time": datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
        "content": "系统通知",
    }
    if decrypted:
        protected = crypto().protect_phone("13800138000")
        value.update(
            phone_enc=protected.phone_enc,
            phone_hmac=protected.phone_hmac,
            key_version=protected.key_version,
        )
    else:
        value["phone_mask"] = "138****8000"
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decrypted", "expected_phone"),
    [(False, "138****8000"), (True, "13800138000")],
)
async def test_worker_builds_masked_or_authorized_csv_without_plaintext_disk(
    tmp_path: Path,
    decrypted: bool,
    expected_phone: str,
) -> None:
    current = claim(decrypted=decrypted)
    repository = FakeRepository(current, [row(decrypted=decrypted)])
    codec = ExportFileCodec(crypto(), tmp_path)
    worker = ExportWorker(repository, codec, crypto())

    assert await worker.process(9) == 1

    assert repository.done is not None and repository.done[3] == 1
    path = Path(str(repository.done[2]))
    assert b"13800138000" not in read_bytes(path)
    plaintext = b"".join(codec.iter_decrypted(path)).decode("utf-8-sig")
    assert expected_phone in plaintext
    assert repository.failed is None


@pytest.mark.asyncio
async def test_worker_marks_failed_and_removes_partial_file_on_limit(tmp_path: Path) -> None:
    current = claim(decrypted=False)
    repository = FakeRepository(current, [row(decrypted=False)] * 3)
    worker = ExportWorker(
        repository,
        ExportFileCodec(crypto(), tmp_path),
        crypto(),
        max_rows=2,
    )

    with pytest.raises(ExportTooLarge):
        await worker.process(9)

    assert repository.failed == (9, current.lease_id)
    assert repository.done is None
    assert os.listdir(tmp_path) == []


@pytest.mark.asyncio
async def test_duplicate_delivery_without_claim_is_noop(tmp_path: Path) -> None:
    repository = FakeRepository(None, [])
    worker = ExportWorker(repository, ExportFileCodec(crypto(), tmp_path), crypto())
    assert await worker.process(9) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decrypted", "expected_phone"),
    [(False, "138****8000"), (True, "13800138000")],
)
async def test_unmatched_worker_uses_reconciliation_header_and_encrypted_file(
    tmp_path: Path,
    decrypted: bool,
    expected_phone: str,
) -> None:
    value: dict[str, object] = {
        "created_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        "custom_id": "legacy-1",
        "vendor_task_id": "vendor-1",
        "report_status": 1,
        "report_desc": "DELIVRD",
        "report_time": datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
    }
    if decrypted:
        protected = crypto().protect_phone("13800138000", table="unmatched_report")
        value.update(
            phone_enc=protected.phone_enc,
            phone_hmac=protected.phone_hmac,
            key_version=protected.key_version,
        )
    else:
        value["phone_mask"] = "138****8000"
    current = unmatched_claim(decrypted=decrypted)
    repository = FakeRepository(current, [value])
    codec = ExportFileCodec(crypto(), tmp_path)

    assert await ExportWorker(repository, codec, crypto()).process(10) == 1

    path = Path(str(repository.done[2]))  # type: ignore[index]
    assert b"13800138000" not in read_bytes(path)
    plaintext = b"".join(codec.iter_decrypted(path)).decode("utf-8-sig")
    assert plaintext.splitlines()[0] == (
        "created_at,custom_id,vendor_task_id,phone,report_status,report_desc,report_time"
    )
    assert expected_phone in plaintext


@pytest.mark.asyncio
async def test_fencing_miss_removes_only_old_worker_file(tmp_path: Path) -> None:
    current = claim(decrypted=False)

    class LostRepository(FakeRepository):
        async def mark_done(
            self,
            task_id: int,
            *,
            lease_id: UUID,
            file_path: str,
            row_count: int,
        ) -> None:
            raise ExportLeaseLost("taken over")

    repository = LostRepository(current, [row(decrypted=False)])
    codec = ExportFileCodec(crypto(), tmp_path)
    newer = UUID("30000000-0000-4000-8000-000000000009")
    newer_path = await codec.write_csv(
        9,
        newer,
        ("phone",),
        _single_row(("139****9000",)),
    )

    with pytest.raises(ExportLeaseLost):
        await ExportWorker(repository, codec, crypto()).process(9)

    assert newer_path.exists()
    assert os.listdir(tmp_path) == [newer_path.name]


async def _single_row(value: tuple[object, ...]) -> AsyncIterator[tuple[object, ...]]:
    yield value
