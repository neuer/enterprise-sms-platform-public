from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from app.services.crypto import CryptoService, EncryptionContext
from app.services.reply_ingest import ReplyIngestService
from app.vendor.zhihui import RawPulledPayload


def crypto() -> CryptoService:
    key = base64.b64encode(b"u" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def rotated_crypto() -> CryptoService:
    first = base64.b64encode(b"u" * 32).decode()
    second = base64.b64encode(b"v" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    return CryptoService.from_secret_values(ring, ring)


class FakeGateway:
    def __init__(self, records: Any) -> None:
        raw = json.dumps({"code": 0, "msg": None, "data": records}).encode()
        self.result = RawPulledPayload(raw, 200)

    async def get_reply_raw(self) -> RawPulledPayload:
        return self.result


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(("persist_raw", values))
        return 23

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
    ) -> None:
        self.events.append(("metadata", (raw_id, custom_ids, item_count)))

    async def store_reply(self, raw_id: int, reply: Any) -> None:
        self.events.append(("store", reply))

    async def mark_processed(self, raw_id: int) -> None:
        self.events.append(("processed", raw_id))

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.events.append(("error", error))


def reply() -> dict[str, Any]:
    return {
        "taskId": "task-1",
        "customId ": "custom-1",
        "phone": "13800138000",
        "extCode": "01",
        "contents": "TD",
        "replyTime": "2026-07-12T08:00:00+08:00",
    }


@pytest.mark.asyncio
async def test_complete_raw_is_committed_before_protected_reply_parsing() -> None:
    repository = FakeRepository()

    assert await ReplyIngestService(FakeGateway([reply()]), repository, crypto()).poll_once() == 1

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "store",
        "processed",
    ]
    raw_values = repository.events[0][1]
    assert raw_values["custom_ids"] == []
    assert raw_values["item_count"] == 0
    assert raw_values["http_status"] == 200
    assert raw_values["content_encoding"] == "identity"
    assert repository.events[1][1] == (23, ["custom-1"], 1)
    assert b"13800138000" not in raw_values["payload_enc"]
    protected = repository.events[2][1]
    assert not hasattr(protected, "phone")
    assert protected.phone_mask == "138****8000"
    assert protected.custom_id == "custom-1"
    assert crypto().decrypt_bound_packed_text(
        protected.content_enc,
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=protected.dedup_hash,
        ),
    ) == "TD"
    assert protected.reply_time.isoformat() == "2026-07-12T08:00:00+08:00"


@pytest.mark.asyncio
async def test_reply_content_masks_embedded_phone_before_projection_persistence() -> None:
    item = reply() | {"contents": "请联系13800138000，备用13900139000"}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    protected = repository.events[2][1]
    assert crypto().decrypt_bound_packed_text(
        protected.content_enc,
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=protected.dedup_hash,
        ),
    ) == "请联系138****8000，备用139****9000"


@pytest.mark.asyncio
async def test_reply_event_key_stays_stable_across_hmac_rotation_and_time_offsets() -> None:
    before = FakeRepository()
    after = FakeRepository()
    same_instant = reply() | {"replyTime": "2026-07-12T00:00:00Z"}

    await ReplyIngestService(FakeGateway([reply()]), before, crypto()).poll_once()
    await ReplyIngestService(
        FakeGateway([same_instant]),
        after,
        rotated_crypto(),
    ).poll_once()

    assert before.events[2][1].dedup_hash == after.events[2][1].dedup_hash
    assert before.events[2][1].dedup_key_version == 1
    assert after.events[2][1].dedup_key_version == 1
    assert before.events[2][1].phone_hmac != after.events[2][1].phone_hmac


@pytest.mark.asyncio
async def test_reply_event_key_is_keyed_and_not_an_offline_content_oracle() -> None:
    repository = FakeRepository()
    await ReplyIngestService(FakeGateway([reply()]), repository, crypto()).poll_once()
    protected = repository.events[2][1]

    unkeyed_guess = hashlib.sha256(
        "\x1f".join(
            (
                "task-1",
                "custom-1",
                crypto().hmac_candidates("13800138000")[1],
                "TD",
                "2026-07-12T00:00:00.000000Z",
            )
        ).encode()
    ).hexdigest()
    assert protected.dedup_hash != unkeyed_guess


@pytest.mark.asyncio
async def test_null_custom_id_is_preserved_without_raw_index_placeholder() -> None:
    item = reply() | {"customId ": None}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (23, [], 1)
    assert repository.events[2][1].custom_id is None


@pytest.mark.asyncio
async def test_parse_failure_keeps_raw_unprocessed_for_replay() -> None:
    repository = FakeRepository()
    broken = reply() | {"contents": "x" * 501}

    with pytest.raises(ValueError, match="contents"):
        await ReplyIngestService(FakeGateway([broken]), repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "metadata", "error"]


@pytest.mark.asyncio
async def test_invalid_reply_data_shape_is_persisted_before_error() -> None:
    repository = FakeRepository()

    with pytest.raises(ValueError, match="object array"):
        await ReplyIngestService(FakeGateway("broken"), repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "error"]
