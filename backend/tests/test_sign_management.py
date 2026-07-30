from __future__ import annotations

from typing import Any

import pytest

from app.services.sign_management import (
    SignManagementService,
    SignRecord,
    SignStateConflict,
    map_vendor_sign_state,
)


class FakeRepository:
    def __init__(self) -> None:
        self.record = SignRecord(1, "青鸾平台", "21", "pending", None)
        self.states: list[tuple[int, str, str | None]] = []

    async def create(self, **values: object) -> SignRecord:
        return SignRecord(2, str(values["name"]), str(values["vendor_sign_id"]), "pending", None)

    async def list_all(self) -> list[SignRecord]:
        return [self.record]

    async def get(self, sign_id: int) -> SignRecord | None:
        return self.record if sign_id == 1 else None

    async def pending(self, sign_id: int | None = None) -> list[SignRecord]:
        if self.record.vendor_state != "pending" or sign_id not in {None, 1}:
            return []
        return [self.record]

    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int:
        self.states.extend(states)
        return len(states)

    async def update(self, sign_id: int, **values: object) -> SignRecord | None:
        if sign_id != self.record.id:
            return None
        self.record = SignRecord(
            sign_id,
            str(values["name"]),
            str(values["vendor_sign_id"]),
            "pending",
            None,
        )
        return self.record

    async def delete(self, sign_id: int, *, actor: str) -> bool:
        return sign_id == self.record.id


class FakeVendor:
    def __init__(self) -> None:
        self.bound: list[str] = []

    async def bind_sign(self, sign_name: str) -> int:
        self.bound.append(sign_name)
        return 22

    async def get_sign_state(self, sign_ids: list[int]) -> list[dict[str, Any]]:
        assert sign_ids == [21]
        return [{"id": 21, "checkType": 2, "checkRemark": "材料不足"}]


@pytest.mark.asyncio
async def test_create_binds_formatted_sign_and_stores_plain_name() -> None:
    vendor = FakeVendor()
    created = await SignManagementService(FakeRepository(), vendor).create(
        name="青鸾平台", actor="admin01"
    )
    assert vendor.bound == ["【青鸾平台】"]
    assert created.name == "青鸾平台"
    assert created.vendor_sign_id == "22"


@pytest.mark.asyncio
async def test_pending_sync_maps_rejection_reason() -> None:
    repository = FakeRepository()
    assert await SignManagementService(repository, FakeVendor()).sync_pending() == 1
    assert repository.states == [(1, "rejected", "材料不足")]


def test_sign_state_mapping_is_strict() -> None:
    assert [map_vendor_sign_state(value) for value in (0, 1, 2)] == [
        "pending",
        "approved",
        "rejected",
    ]
    with pytest.raises(ValueError):
        map_vendor_sign_state(3)


@pytest.mark.asyncio
async def test_manual_sync_rejects_nonpending_sign() -> None:
    repository = FakeRepository()
    repository.record = SignRecord(1, "青鸾平台", "21", "approved", None)
    with pytest.raises(SignStateConflict):
        await SignManagementService(repository, FakeVendor()).sync_pending(sign_id=1)
