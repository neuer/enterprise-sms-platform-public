from __future__ import annotations

from typing import Any

import pytest

from app.services.template_management import (
    TemplateManagementService,
    TemplateRecord,
    TemplateStateConflict,
    map_vendor_template_state,
)


class FakeRepository:
    def __init__(self) -> None:
        self.records = {
            1: TemplateRecord(
                1,
                "验证码",
                "验证码{1}",
                [{"pos": 1, "max_len": 6}],
                "平台部",
                "21",
                "pending",
                None,
            )
        }
        self.created: tuple[object, ...] | None = None
        self.states: list[tuple[int, str, str | None]] = []

    async def create(self, **values: object) -> TemplateRecord:
        self.created = tuple(values.values())
        record = TemplateRecord(
            2,
            str(values["name"]),
            str(values["content"]),
            values["var_specs"],  # type: ignore[arg-type]
            str(values["dept"]),
            None,
            "pending",
            None,
        )
        self.records[2] = record
        return record

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        return [record for record in self.records.values() if dept is None or record.dept == dept]

    async def get(self, template_id: int, *, dept: str | None = None) -> TemplateRecord | None:
        record = self.records.get(template_id)
        return record if record is not None and (dept is None or record.dept == dept) else None

    async def pending(self, template_id: int | None = None) -> list[TemplateRecord]:
        return [
            item
            for item in self.records.values()
            if item.vendor_state == "pending" and (template_id is None or item.id == template_id)
        ]

    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int:
        self.states.extend(states)
        return len(states)

    async def apply_binding(self, template_id: int, vendor_template_id: str) -> bool:
        current = self.records.get(template_id)
        if current is None or current.vendor_template_id is not None:
            return False
        self.records[template_id] = TemplateRecord(
            current.id,
            current.name,
            current.content,
            current.var_specs,
            current.dept,
            vendor_template_id,
            current.vendor_state,
            current.vendor_reject_reason,
        )
        return True

    async def update(self, template_id: int, **values: object) -> TemplateRecord | None:
        current = self.records[template_id]
        if current.vendor_state not in {"draft", "rejected"}:
            return None
        updated = TemplateRecord(
            template_id,
            str(values["name"]),
            str(values["content"]),
            values["var_specs"],  # type: ignore[arg-type]
            current.dept,
            None,
            "pending",
            None,
        )
        self.records[template_id] = updated
        return updated

    async def delete(self, template_id: int, *, actor: str) -> bool:
        return self.records.pop(template_id, None) is not None


class FakeVendor:
    def __init__(self) -> None:
        self.bound: list[str] = []

    async def bind_template(self, template_content: str) -> int:
        self.bound.append(template_content)
        return 22

    async def get_template_state(self, template_ids: list[int]) -> list[dict[str, Any]]:
        assert template_ids == [21]
        return [{"id": 21, "checkType": 1, "checkRemark": None}]


@pytest.mark.asyncio
async def test_create_validates_placeholders_without_using_vendor_client() -> None:
    repository = FakeRepository()
    vendor = FakeVendor()
    record = await TemplateManagementService(repository, vendor).create(
        name="业务提醒",
        content="尊敬的{1}，验证码{2}",
        var_specs=[{"pos": 1, "max_len": 10}, {"pos": 2, "max_len": 6}],
        dept="平台部",
        actor="operator01",
    )
    assert vendor.bound == []
    assert record.vendor_template_id is None
    assert record.vendor_state == "pending"


@pytest.mark.asyncio
async def test_bind_worker_converts_placeholders_and_applies_vendor_id() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        None,
        "pending",
        None,
    )
    vendor = FakeVendor()
    assert await TemplateManagementService(repository, vendor).bind(1) == 1
    assert vendor.bound == ["验证码{s6}"]
    assert repository.records[1].vendor_template_id == "22"


@pytest.mark.asyncio
async def test_sync_queries_pending_only_and_maps_vendor_state() -> None:
    repository = FakeRepository()
    count = await TemplateManagementService(repository, FakeVendor()).sync_pending()
    assert count == 1
    assert repository.states == [(1, "approved", None)]


def test_vendor_template_state_mapping_is_strict() -> None:
    assert map_vendor_template_state(0) == "pending"
    assert map_vendor_template_state(1) == "approved"
    assert map_vendor_template_state(2) == "rejected"
    with pytest.raises(ValueError):
        map_vendor_template_state(9)


@pytest.mark.asyncio
async def test_manual_sync_rejects_non_pending_template() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        "21",
        "approved",
        None,
    )
    with pytest.raises(TemplateStateConflict):
        await TemplateManagementService(repository, FakeVendor()).sync_pending(template_id=1)


@pytest.mark.asyncio
async def test_rejected_template_can_be_updated_and_resubmitted() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "旧模板",
        "旧{1}",
        [{"pos": 1, "max_len": 2}],
        "平台部",
        "21",
        "rejected",
        "格式不符",
    )
    updated = await TemplateManagementService(repository, FakeVendor()).update(
        1,
        name="新模板",
        content="新{1}",
        var_specs=[{"pos": 1, "max_len": 8}],
        actor="operator01",
    )
    assert updated.vendor_state == "pending"
    assert updated.vendor_reject_reason is None


@pytest.mark.asyncio
async def test_delete_removes_unreferenced_nonapproved_template() -> None:
    repository = FakeRepository()
    await TemplateManagementService(repository, FakeVendor()).delete(1, actor="operator01")
    assert repository.records == {}
