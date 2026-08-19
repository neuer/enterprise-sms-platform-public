from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from app.services.crypto import CryptoService, EncryptionContext
from app.services.reply_ingest import ReplyIngestService
from app.vendor.zhihui import RawPulledPayload, VendorResponseTooLarge


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

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
        return [value for value in custom_ids if value == "custom1"]

    async def store_reply(self, raw_id: int, reply: Any) -> None:
        self.events.append(("store", reply))

    async def mark_processed(self, raw_id: int) -> None:
        self.events.append(("processed", raw_id))

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.events.append(("error", error))


def reply() -> dict[str, Any]:
    return {
        "taskId": "task-1",
        "customId ": "custom1",
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
    assert repository.events[1][1] == (23, ["custom1"], 1)
    assert b"13800138000" not in raw_values["payload_enc"]
    protected = repository.events[2][1]
    assert not hasattr(protected, "phone")
    assert protected.phone_mask == "138****8000"
    assert protected.match_custom_id == "custom1"
    assert protected.custom_id is not None and len(protected.custom_id) == 64
    assert len(protected.vendor_task_id) == 64
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
async def test_optout_intent_is_flagged_on_masked_content() -> None:
    cases = {
        "TD": True,
        "退订": True,
        " t ": True,
        "td": True,
        "TDTD": False,
        "请问怎么退订？": False,
        "已收到": False,
    }
    for content, expected in cases.items():
        repository = FakeRepository()
        item = reply() | {"contents": content}

        await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

        protected = repository.events[2][1]
        assert protected.is_optout is expected, content


@pytest.mark.asyncio
async def test_phone_like_reply_task_id_degrades_to_pseudonym() -> None:
    item = reply() | {"taskId": "13800138000"}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    stored = [value for event, value in repository.events if event == "store"]
    assert len(stored) == 1
    assert len(stored[0].vendor_task_id) == 64
    assert "13800138000" not in stored[0].vendor_task_id
    assert ("processed", 23) in repository.events
    assert not any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_phone_like_reply_custom_id_is_dropped_without_plaintext() -> None:
    item = reply() | {"customId ": "13800138000"}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    stored = [value for event, value in repository.events if event == "store"]
    assert len(stored) == 1
    assert stored[0].custom_id is None
    assert stored[0].match_custom_id is None
    assert ("processed", 23) in repository.events
    assert not any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_empty_reply_custom_id_is_stored_without_pseudonym() -> None:
    item = reply() | {"customId ": ""}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    stored = [value for event, value in repository.events if event == "store"]
    assert len(stored) == 1
    assert stored[0].custom_id is None
    assert stored[0].match_custom_id is None
    assert ("processed", 23) in repository.events


@pytest.mark.asyncio
async def test_invalid_custom_id_does_not_abort_valid_reply_items() -> None:
    repository = FakeRepository()
    mixed = [reply() | {"customId ": "legacy-x"}, reply()]

    await ReplyIngestService(FakeGateway(mixed), repository, crypto()).poll_once()

    assert repository.events[1][1] == (23, ["custom1"], 2)
    stored = [value for event, value in repository.events if event == "store"]
    assert len(stored) == 2
    assert stored[0].custom_id is None
    assert stored[0].match_custom_id is None
    assert stored[1].match_custom_id == "custom1"
    assert not any(event[0] == "error" for event in repository.events)
    assert ("processed", 23) in repository.events


class OversizedReplyGateway:
    def __init__(self, error: VendorResponseTooLarge) -> None:
        self.error = error

    async def get_reply_raw(self) -> RawPulledPayload:
        raise self.error


@pytest.mark.asyncio
async def test_oversized_reply_fallback_persists_read_bytes_with_valid_status() -> None:
    repository = FakeRepository()
    gateway = OversizedReplyGateway(
        VendorResponseTooLarge("too large", raw_body=b'{"code":0,"data":[', status_code=200),
    )

    with pytest.raises(VendorResponseTooLarge):
        await ReplyIngestService(gateway, repository, crypto()).poll_once()

    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert len(persisted) == 1
    assert persisted[0]["http_status"] == 200
    assert b'{"code":0,"data":[' not in persisted[0]["payload_enc"]
    assert any(event[0] == "error" for event in repository.events)


@pytest.mark.asyncio
async def test_oversized_reply_without_body_only_alerts() -> None:
    repository = FakeRepository()
    gateway = OversizedReplyGateway(VendorResponseTooLarge("headers too large"))

    with pytest.raises(VendorResponseTooLarge):
        await ReplyIngestService(gateway, repository, crypto()).poll_once()

    assert not any(event[0] == "persist_raw" for event in repository.events)


@pytest.mark.asyncio
async def test_reply_ext_code_enforces_vendor_digit_contract() -> None:
    repository = FakeRepository()

    await ReplyIngestService(
        FakeGateway([reply() | {"extCode": "12AB"}]),
        repository,
        crypto(),
    ).poll_once()

    assert not any(event[0] == "store" for event in repository.events)
    assert any(event[0] == "error" for event in repository.events)
    assert ("processed", 23) not in repository.events


@pytest.mark.asyncio
async def test_reply_ext_code_is_not_persisted_as_plaintext_otp_metadata() -> None:
    repository = FakeRepository()

    await ReplyIngestService(
        FakeGateway([reply() | {"extCode": "123456"}]),
        repository,
        crypto(),
    ).poll_once()

    protected = next(value for event, value in repository.events if event == "store")
    assert protected.ext_code == ""


@pytest.mark.asyncio
async def test_reply_identifier_text_is_only_persisted_as_hmac_pseudonym() -> None:
    repository = FakeRepository()
    item = reply() | {"taskId": "OTP123456", "customId ": "SecretOTP123456"}

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (23, [], 1)
    protected = next(value for event, value in repository.events if event == "store")
    assert protected.match_custom_id == "SecretOTP123456"
    assert len(protected.vendor_task_id) == 64
    assert protected.custom_id is not None and len(protected.custom_id) == 64
    assert "OTP123456" not in protected.vendor_task_id + protected.custom_id


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
                "custom1",
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
async def test_empty_string_custom_id_is_ingested_as_null() -> None:
    """厂商对旧发送/无关联上行返回 customId=""，必须照常入库而不是判为无法解析。"""

    item = reply() | {"customId ": ""}
    repository = FakeRepository()

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    assert repository.events[1][1] == (23, [], 1)
    stored = next(value for event, value in repository.events if event == "store")
    assert stored.custom_id is None
    assert [event[0] for event in repository.events][-1] == "processed"


@pytest.mark.asyncio
async def test_parse_failure_skips_item_and_keeps_raw_replayable() -> None:
    repository = FakeRepository()
    broken = reply() | {"contents": "x" * 501}

    await ReplyIngestService(FakeGateway([broken]), repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "error",
    ]


@pytest.mark.asyncio
async def test_invalid_reply_data_shape_is_persisted_before_error() -> None:
    repository = FakeRepository()

    with pytest.raises(ValueError, match="object array"):
        await ReplyIngestService(FakeGateway("broken"), repository, crypto()).poll_once()

    assert [event[0] for event in repository.events] == ["persist_raw", "error"]


@pytest.mark.asyncio
async def test_naive_reply_time_is_interpreted_as_shanghai_local() -> None:
    repository = FakeRepository()
    item = reply() | {"replyTime": "2026-07-12 08:00:00"}

    await ReplyIngestService(FakeGateway([item]), repository, crypto()).poll_once()

    stored = next(value for event, value in repository.events if event == "store")
    assert stored.reply_time.tzinfo is not None
    assert stored.reply_time.utcoffset() is not None


class BrokenReplySpill:
    """磁盘满/权限错等 spill 故障；不得反向阻断 DB 落库。"""

    def write(self, **values: Any) -> None:
        raise OSError("no space left on device")

    def remove(self, source: str, payload_sha256: str) -> None:
        raise OSError("no space left on device")

    def list_pending(self) -> list[Any]:
        return []


class RecordingAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


@pytest.mark.asyncio
async def test_reply_spill_write_failure_degrades_to_alert_and_db_persist_continues() -> None:
    repository = FakeRepository()
    alerts = RecordingAlerts()
    service = ReplyIngestService(
        FakeGateway([reply()]),
        repository,
        crypto(),
        alerts=alerts,
        spill=BrokenReplySpill(),  # type: ignore[arg-type]
    )

    assert await service.poll_once() == 1

    assert [event[0] for event in repository.events] == [
        "persist_raw",
        "metadata",
        "store",
        "processed",
    ]
    spill_alerts = [
        event for event in alerts.events if event["alert_type"] == "vendor_raw_spill_failed"
    ]
    assert len(spill_alerts) == 1
    assert spill_alerts[0]["level"] == "crit"
