from __future__ import annotations

from typing import Any

import pytest

from app.services.sign_management import (
    SignManagementService,
    SignRecord,
    SignStateConflict,
    SignStateUpdate,
    map_vendor_sign_state,
)


class FakeRepository:
    def __init__(self) -> None:
        self.record = SignRecord(1, "青鸾平台", "21", "pending", None)
        self.states: list[SignStateUpdate] = []
        self.adoptions: list[tuple[int, str, str, str | None]] = []

    async def create(self, **values: object) -> SignRecord:
        return SignRecord(2, str(values["name"]), None, "pending", None)

    async def list_all(self) -> list[SignRecord]:
        return [self.record]

    async def get(self, sign_id: int) -> SignRecord | None:
        return self.record if sign_id == 1 else None

    async def syncable(self, sign_id: int | None = None) -> list[SignRecord]:
        if self.record.vendor_sign_id is None or sign_id not in {None, 1}:
            return []
        return [self.record]

    async def apply_states(self, states: list[SignStateUpdate]) -> int:
        self.states.extend(states)
        return len(states)

    async def apply_binding(self, sign_id: int, vendor_sign_id: str) -> bool:
        if sign_id != self.record.id or self.record.vendor_sign_id is not None:
            return False
        self.record = SignRecord(
            self.record.id,
            self.record.name,
            vendor_sign_id,
            self.record.vendor_state,
            self.record.vendor_reject_reason,
        )
        return True

    async def adopt_existing(
        self,
        sign_id: int,
        vendor_sign_id: str,
        vendor_state: str,
        vendor_reject_reason: str | None,
    ) -> bool:
        if sign_id != self.record.id or self.record.vendor_sign_id is not None:
            return False
        self.adoptions.append(
            (sign_id, vendor_sign_id, vendor_state, vendor_reject_reason)
        )
        self.record = SignRecord(
            self.record.id,
            self.record.name,
            vendor_sign_id,
            vendor_state,
            vendor_reject_reason,
        )
        return True

    async def update(self, sign_id: int, **values: object) -> SignRecord | None:
        if sign_id != self.record.id:
            return None
        self.record = SignRecord(
            sign_id,
            str(values["name"]),
            None,
            "pending",
            None,
        )
        return self.record

    async def delete(self, sign_id: int, *, actor: str) -> bool:
        return sign_id == self.record.id


class FakeVendor:
    def __init__(
        self,
        *,
        state_id: int = 21,
        check_type: int = 2,
        reason: str | None = "材料不足",
        response: list[dict[str, Any]] | None = None,
    ) -> None:
        self.bound: list[str] = []
        self.state_id = state_id
        self.check_type = check_type
        self.reason = reason
        self.response = response
        self.queried: list[list[int]] = []

    async def bind_sign(self, sign_name: str) -> int:
        self.bound.append(sign_name)
        return 22

    async def get_sign_state(self, sign_ids: list[int]) -> list[dict[str, Any]]:
        self.queried.append(sign_ids)
        if self.response is not None:
            return self.response
        return [
            {
                "id": self.state_id,
                "checkType": self.check_type,
                "checkRemark": self.reason,
            }
        ]


@pytest.mark.asyncio
async def test_create_formats_and_persists_without_using_vendor_client() -> None:
    vendor = FakeVendor()
    created = await SignManagementService(FakeRepository(), vendor).create(
        name="青鸾平台", actor="admin01"
    )
    assert vendor.bound == []
    assert created.name == "青鸾平台"
    assert created.vendor_sign_id is None


@pytest.mark.asyncio
async def test_bind_worker_submits_formatted_sign_and_applies_vendor_id() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "青鸾平台", None, "pending", None)
    vendor = FakeVendor()
    assert await SignManagementService(repository, vendor).bind(1) == 1
    assert vendor.bound == ["【青鸾平台】"]
    assert repository.record.vendor_sign_id == "22"


@pytest.mark.asyncio
async def test_pending_sync_maps_rejection_reason() -> None:
    repository = FakeRepository()
    assert await SignManagementService(repository, FakeVendor()).sync_pending() == 1
    assert repository.states == [(1, "21", 0, "rejected", "材料不足")]


def test_sign_state_mapping_is_strict() -> None:
    assert [map_vendor_sign_state(value) for value in (0, 1, 2)] == [
        "pending",
        "approved",
        "rejected",
    ]
    for invalid in (3, True, 1.0):
        with pytest.raises(ValueError):
            map_vendor_sign_state(invalid)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_approved_sign_can_refresh_to_rejected() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "青鸾平台", "21", "approved", None, 51)

    assert await SignManagementService(repository, FakeVendor()).sync_pending(sign_id=1) == 1
    assert repository.states == [(1, "21", 51, "rejected", "材料不足")]


@pytest.mark.asyncio
async def test_approved_sign_clears_stale_reason_and_skips_exact_noop() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "青鸾平台", "21", "approved", "旧原因")
    vendor = FakeVendor(check_type=1, reason="厂商仍返回旧原因")

    assert await SignManagementService(repository, vendor).sync_pending() == 1
    assert repository.states == [(1, "21", 0, "approved", None)]

    repository.states.clear()
    repository.record = SignRecord(1, "青鸾平台", "21", "approved", None)
    assert await SignManagementService(repository, vendor).sync_pending() == 0
    assert repository.states == []


@pytest.mark.asyncio
async def test_manual_sync_rejects_unbound_sign() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "青鸾平台", None, "pending", None)
    with pytest.raises(SignStateConflict, match="厂商编号"):
        await SignManagementService(repository, FakeVendor()).sync_pending(sign_id=1)


@pytest.mark.asyncio
async def test_sign_sync_rejects_incomplete_vendor_batch_before_write() -> None:
    repository = FakeRepository()

    with pytest.raises(ValueError, match="incomplete"):
        await SignManagementService(
            repository,
            FakeVendor(response=[]),
        ).sync_pending()

    assert repository.states == []


@pytest.mark.asyncio
async def test_prepare_adoption_requires_unbound_pending_and_exact_name() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "厦门钨业", None, "pending", None)
    service = SignManagementService(repository)

    assert (await service.prepare_adoption(1, confirmed_name="厦门钨业")).id == 1
    with pytest.raises(SignStateConflict, match="名称"):
        await service.prepare_adoption(1, confirmed_name="其他签名")

    repository.record = SignRecord(1, "厦门钨业", "112074", "pending", None)
    with pytest.raises(SignStateConflict, match="尚未绑定"):
        await service.prepare_adoption(1, confirmed_name="厦门钨业")


@pytest.mark.asyncio
async def test_adopt_existing_queries_exact_id_and_applies_vendor_state() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "厦门钨业", None, "pending", None)
    vendor = FakeVendor(state_id=112074, check_type=1, reason="应被清理的旧原因")

    assert await SignManagementService(repository, vendor).adopt_existing(1, 112074) == 1
    assert vendor.queried == [[112074]]
    assert repository.adoptions == [(1, "112074", "approved", None)]
    assert repository.record.vendor_state == "approved"


@pytest.mark.asyncio
async def test_adopt_existing_rejects_mismatched_vendor_response() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "厦门钨业", None, "pending", None)
    vendor = FakeVendor(state_id=112075, check_type=1, reason=None)

    with pytest.raises(ValueError, match="unexpected GetSignState id"):
        await SignManagementService(repository, vendor).adopt_existing(1, 112074)
    assert repository.adoptions == []


@pytest.mark.asyncio
async def test_adopt_existing_rejects_duplicate_vendor_response() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "厦门钨业", None, "pending", None)
    response = [
        {"id": 112074, "checkType": 1, "checkRemark": None},
        {"id": 112074, "checkType": 1, "checkRemark": None},
    ]

    with pytest.raises(ValueError, match="duplicate GetSignState id"):
        await SignManagementService(
            repository,
            FakeVendor(response=response),
        ).adopt_existing(1, 112074)
    assert repository.adoptions == []
