from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.services.crypto import CryptoService

NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)
PHONE = "13800138000"


def _crypto() -> CryptoService:
    return CryptoService(
        aes_keys={1: b"a" * 32, 2: b"b" * 32},
        hmac_keys={1: b"c" * 32, 2: b"d" * 32},
        active_version=2,
    )


class MemoryRepository:
    def __init__(self) -> None:
        self.records: dict[int, object] = {}
        self.next_id = 1
        self.busy = False
        self.duplicate = False
        self.candidates: dict[int, str] = {}
        self.aliases: dict[int, dict[int, str]] = {}

    async def create(self, candidate: object, candidates: dict[int, str], *, actor: str):
        from app.services.vendor_test_recipient import (
            DuplicateVendorTestRecipient,
            VendorTestRecipientRecord,
        )

        self.candidates = candidates
        if self.duplicate:
            raise DuplicateVendorTestRecipient("测试号码已登记")
        record = VendorTestRecipientRecord(
            id=self.next_id,
            label=candidate.label,
            phone_enc=candidate.phone_enc,
            phone_hmac=candidate.phone_hmac,
            phone_mask=candidate.phone_mask,
            key_version=candidate.key_version,
            status="active",
            created_by=actor,
            created_at=NOW,
        )
        self.records[record.id] = record
        self.aliases[record.id] = dict(candidates)
        self.next_id += 1
        return record

    async def list_summaries(self, *, include_disabled: bool = True):
        from app.services.vendor_test_recipient import VendorTestRecipientSummary

        return tuple(
            VendorTestRecipientSummary(
                id=item.id,
                label=item.label,
                phone_mask=item.phone_mask,
                status=item.status,
                created_at=item.created_at,
                disabled_at=item.disabled_at,
            )
            for item in self.records.values()
            if include_disabled or item.status == "active"
        )

    async def disable(self, recipient_id: int, *, actor: str):
        from app.services.vendor_test_recipient import (
            RecipientBusy,
            RecipientNotFound,
            VendorTestRecipientSummary,
        )

        if self.busy:
            raise RecipientBusy("测试号码存在活动任务")
        item = self.records.get(recipient_id)
        if item is None:
            raise RecipientNotFound("测试号码不存在")
        item = replace(item, status="disabled", disabled_by=actor, disabled_at=NOW)
        self.records[recipient_id] = item
        return VendorTestRecipientSummary(
            item.id,
            item.label,
            item.phone_mask,
            item.status,
            item.created_at,
            item.disabled_at,
        )

    async def delete(self, recipient_id: int, *, actor: str) -> bool:
        from app.services.vendor_test_recipient import RecipientBusy

        if self.busy:
            raise RecipientBusy("测试号码存在活动任务")
        return self.records.pop(recipient_id, None) is not None

    async def purge_all(self, *, actor: str) -> int:
        from app.services.vendor_test_recipient import RecipientBusy

        if self.busy:
            raise RecipientBusy("测试号码存在活动任务")
        count = len(self.records)
        self.records.clear()
        self.aliases.clear()
        return count

    async def resolve_for_send(self, recipient_id: int):
        from app.services.vendor_test_recipient import (
            RecipientNotFound,
            VendorTestRecipientForSend,
        )

        item = self.records.get(recipient_id)
        if item is None or item.status != "active":
            raise RecipientNotFound("测试号码不存在或已停用")
        return VendorTestRecipientForSend(
            item.id,
            item.phone_enc,
            item.phone_hmac,
            item.phone_mask,
            item.key_version,
            tuple(sorted(self.aliases.get(recipient_id, {}).items())),
        )

    async def resolve_by_hmac_candidates(self, candidates: dict[int, str]):
        self.candidates = dict(candidates)
        for recipient_id, aliases in self.aliases.items():
            if any(aliases.get(version) == digest for version, digest in candidates.items()):
                return await self.resolve_for_send(recipient_id)
        from app.services.vendor_test_recipient import RecipientNotFound

        raise RecipientNotFound("测试号码不存在或已停用")

    async def refresh_hmac_candidates(
        self,
        recipient_id: int,
        candidates: dict[int, str],
        *,
        actor: str,
    ):
        from app.services.vendor_test_recipient import (
            InvalidVendorTestRecipient,
            RecipientNotFound,
            VendorTestRecipientSummary,
        )

        item = self.records.get(recipient_id)
        if item is None:
            raise RecipientNotFound("测试号码不存在")
        existing = self.aliases.get(recipient_id, {})
        if not any(existing.get(version) == digest for version, digest in candidates.items()):
            raise InvalidVendorTestRecipient("输入号码与登记记录不匹配")
        self.aliases[recipient_id] = dict(candidates)
        return VendorTestRecipientSummary(
            item.id,
            item.label,
            item.phone_mask,
            item.status,
            item.created_at,
            item.disabled_at,
        )


@pytest.mark.asyncio
async def test_add_protects_phone_and_checks_all_hmac_key_versions() -> None:
    from app.services.vendor_test_recipient import VendorTestRecipientService

    repository = MemoryRepository()
    crypto = _crypto()
    service = VendorTestRecipientService(repository, crypto)

    record = await service.add(label=" 值班测试机 ", phone=PHONE, actor="admin")

    assert record.label == "值班测试机"
    assert record.phone_mask == "138****8000"
    assert record.phone_hmac == crypto.phone_hmac(PHONE)
    assert PHONE.encode() not in record.phone_enc
    assert repository.candidates == crypto.hmac_candidates(PHONE)


@pytest.mark.asyncio
async def test_add_rejects_invalid_phone_label_and_duplicate_without_exposing_phone() -> None:
    from app.services.vendor_test_recipient import (
        DuplicateVendorTestRecipient,
        InvalidVendorTestRecipient,
        VendorTestRecipientService,
    )

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())

    with pytest.raises(InvalidVendorTestRecipient) as invalid_phone:
        await service.add(label="测试机", phone="1380013800", actor="admin")
    assert "1380013800" not in str(invalid_phone.value)

    with pytest.raises(InvalidVendorTestRecipient):
        await service.add(label=" ", phone=PHONE, actor="admin")
    with pytest.raises(InvalidVendorTestRecipient):
        await service.add(label="x" * 65, phone=PHONE, actor="admin")

    repository.duplicate = True
    with pytest.raises(DuplicateVendorTestRecipient) as duplicate:
        await service.add(label="测试机", phone=PHONE, actor="admin")
    assert PHONE not in str(duplicate.value)
    assert _crypto().phone_hmac(PHONE) not in str(duplicate.value)


@pytest.mark.asyncio
async def test_list_returns_only_safe_summaries_and_can_filter_disabled() -> None:
    from app.services.vendor_test_recipient import VendorTestRecipientService

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    first = await service.add(label="一号机", phone=PHONE, actor="admin")
    await service.add(label="二号机", phone="13900139000", actor="admin")
    await service.disable(first.id, actor="admin")

    all_items = await service.list(include_disabled=True)
    active_items = await service.list(include_disabled=False)

    assert len(all_items) == 2 and len(active_items) == 1
    assert not hasattr(all_items[0], "phone_enc")
    assert not hasattr(all_items[0], "phone_hmac")


@pytest.mark.asyncio
async def test_busy_recipient_cannot_be_disabled_or_deleted() -> None:
    from app.services.vendor_test_recipient import RecipientBusy, VendorTestRecipientService

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    record = await service.add(label="测试机", phone=PHONE, actor="admin")
    repository.busy = True

    with pytest.raises(RecipientBusy):
        await service.disable(record.id, actor="admin")
    with pytest.raises(RecipientBusy):
        await service.delete(record.id, actor="admin")


@pytest.mark.asyncio
async def test_purge_all_delegates_without_decrypting_recipients() -> None:
    from app.services.vendor_test_recipient import VendorTestRecipientService

    repository = MemoryRepository()
    crypto = _crypto()
    service = VendorTestRecipientService(repository, crypto)
    await service.add(label="一号机", phone=PHONE, actor="admin")
    await service.add(label="二号机", phone="13900139000", actor="admin")

    original_decrypt = crypto.decrypt_text
    crypto.decrypt_text = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("purge must not decrypt")
    )
    try:
        assert await service.purge_all(actor="admin") == 2
    finally:
        crypto.decrypt_text = original_decrypt

    assert repository.records == {}
    assert repository.aliases == {}


@pytest.mark.asyncio
async def test_resolve_for_send_returns_protected_tuple_without_plaintext() -> None:
    from app.services.vendor_test_recipient import VendorTestRecipientService

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    record = await service.add(label="测试机", phone=PHONE, actor="admin")

    resolved = await service.resolve_for_send(record.id)

    assert resolved.phone_enc == record.phone_enc
    assert resolved.phone_hmac == record.phone_hmac
    assert resolved.phone_mask == record.phone_mask
    assert resolved.key_version == record.key_version
    assert dict(resolved.hmac_candidates) == _crypto().hmac_candidates(PHONE)
    assert PHONE not in repr(resolved)


@pytest.mark.asyncio
async def test_resolve_phone_for_api_uat_uses_hmac_candidates_and_returns_only_protected_data() -> (
    None
):
    from app.services.vendor_test_recipient import VendorTestRecipientService

    repository = MemoryRepository()
    crypto = _crypto()
    service = VendorTestRecipientService(repository, crypto)
    record = await service.add(label="测试机", phone=PHONE, actor="admin")

    resolved = await service.resolve_phone_for_send(PHONE)

    assert repository.candidates == crypto.hmac_candidates(PHONE)
    assert resolved.id == record.id
    assert resolved.phone_enc == record.phone_enc
    assert resolved.hmac_candidates == tuple(sorted(crypto.hmac_candidates(PHONE).items()))
    assert PHONE not in repr(resolved)


@pytest.mark.asyncio
async def test_resolve_fails_closed_when_hmac_alias_index_is_stale() -> None:
    from app.services.vendor_test_recipient import (
        RecipientHmacIndexStale,
        VendorTestRecipientService,
    )

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    record = await service.add(label="测试机", phone=PHONE, actor="admin")
    repository.aliases[record.id].pop(1)

    with pytest.raises(RecipientHmacIndexStale):
        await service.resolve_for_send(record.id)


@pytest.mark.asyncio
async def test_api_phone_resolve_requires_every_hmac_digest_to_match_input() -> None:
    from app.services.vendor_test_recipient import (
        RecipientHmacIndexStale,
        VendorTestRecipientService,
    )

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    record = await service.add(label="测试机", phone=PHONE, actor="admin")
    repository.aliases[record.id][1] = "f" * 64

    with pytest.raises(RecipientHmacIndexStale):
        await service.resolve_phone_for_send(PHONE)


@pytest.mark.asyncio
async def test_refresh_hmac_index_requires_reentered_matching_phone() -> None:
    from app.services.vendor_test_recipient import (
        InvalidVendorTestRecipient,
        VendorTestRecipientService,
    )

    repository = MemoryRepository()
    service = VendorTestRecipientService(repository, _crypto())
    record = await service.add(label="测试机", phone=PHONE, actor="admin")
    repository.aliases[record.id] = {2: _crypto().phone_hmac(PHONE, 2)}

    with pytest.raises(InvalidVendorTestRecipient):
        await service.refresh_hmac_index(
            record.id,
            phone="13900139000",
            actor="admin",
        )

    refreshed = await service.refresh_hmac_index(
        record.id,
        phone=PHONE,
        actor="admin",
    )
    assert refreshed.phone_mask == "138****8000"
    assert repository.aliases[record.id] == _crypto().hmac_candidates(PHONE)
