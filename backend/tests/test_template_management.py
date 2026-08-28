from __future__ import annotations

from typing import Any

import pytest

from app.services.template_management import (
    TemplateManagementService,
    TemplateRecord,
    TemplateStateConflict,
    TemplateStateUpdate,
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
        self.states: list[TemplateStateUpdate] = []

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

    async def syncable(self, template_id: int | None = None) -> list[TemplateRecord]:
        return [
            item
            for item in self.records.values()
            if item.vendor_state in {"pending", "approved", "rejected"}
            and item.vendor_template_id is not None
            and (template_id is None or item.id == template_id)
        ]

    async def apply_states(self, states: list[TemplateStateUpdate]) -> int:
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
        if (
            current.vendor_state not in {"draft", "rejected"}
            or current.vendor_template_id is not None
        ):
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
        current = self.records.get(template_id)
        if current is None or current.vendor_template_id is not None:
            return False
        return self.records.pop(template_id, None) is not None


class FakeVendor:
    def __init__(
        self,
        *,
        check_type: int = 1,
        remark: str | None = None,
        response: list[dict[str, Any]] | None = None,
        bind_id: int = 22,
    ) -> None:
        self.bound: list[str] = []
        self.check_type = check_type
        self.remark = remark
        self.response = response
        self.bind_id = bind_id
        self.queried: list[list[int]] = []

    async def bind_template(self, template_content: str) -> int:
        self.bound.append(template_content)
        return self.bind_id

    async def get_template_state(self, template_ids: list[int]) -> list[dict[str, Any]]:
        self.queried.append(template_ids)
        if self.response is not None:
            return self.response
        assert template_ids == [21]
        return [{"id": 21, "checkType": self.check_type, "checkRemark": self.remark}]


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


@pytest.mark.parametrize("vendor_id", [0, -1, 2_147_483_648, True])
@pytest.mark.asyncio
async def test_bind_rejects_invalid_vendor_id(vendor_id: int) -> None:
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

    with pytest.raises(ValueError, match="invalid template id"):
        await TemplateManagementService(
            repository,
            FakeVendor(bind_id=vendor_id),
        ).bind(1)


@pytest.mark.asyncio
async def test_sync_queries_syncable_templates_and_maps_vendor_state() -> None:
    repository = FakeRepository()
    count = await TemplateManagementService(repository, FakeVendor()).sync_pending()
    assert count == 1
    assert repository.states == [(1, "21", 0, "approved", None)]


@pytest.mark.parametrize(
    ("source_state", "check_type", "target_state", "target_reason"),
    (
        ("pending", 1, "approved", None),
        ("pending", 2, "rejected", "厂商新原因"),
        ("approved", 0, "pending", None),
        ("approved", 2, "rejected", "厂商新原因"),
        ("rejected", 0, "pending", None),
        ("rejected", 1, "approved", None),
    ),
)
@pytest.mark.asyncio
async def test_bound_template_supports_all_vendor_state_transitions(
    source_state: str,
    check_type: int,
    target_state: str,
    target_reason: str | None,
) -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        "21",
        source_state,
        "历史原因" if source_state == "rejected" else None,
        12,
    )

    count = await TemplateManagementService(
        repository,
        FakeVendor(check_type=check_type, remark="厂商新原因"),
    ).sync_pending()

    assert count == 1
    assert repository.states == [(1, "21", 12, target_state, target_reason)]


def test_vendor_template_state_mapping_is_strict() -> None:
    assert map_vendor_template_state(0) == "pending"
    assert map_vendor_template_state(1) == "approved"
    assert map_vendor_template_state(2) == "rejected"
    for invalid in (9, True, 1.0):
        with pytest.raises(ValueError):
            map_vendor_template_state(invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("vendor_state", "vendor_template_id"),
    (("draft", "21"), ("pending", None)),
)
@pytest.mark.asyncio
async def test_manual_sync_rejects_unbound_template(
    vendor_state: str,
    vendor_template_id: str | None,
) -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        vendor_template_id,
        vendor_state,
        None,
    )
    with pytest.raises(TemplateStateConflict):
        await TemplateManagementService(repository, FakeVendor()).sync_pending(template_id=1)


@pytest.mark.asyncio
async def test_rejected_template_can_refresh_to_approved() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        "21",
        "rejected",
        "材料不足",
    )

    count = await TemplateManagementService(repository, FakeVendor()).sync_pending(template_id=1)

    assert count == 1
    assert repository.states == [(1, "21", 0, "approved", None)]


@pytest.mark.asyncio
async def test_approved_template_can_refresh_to_rejected() -> None:
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
        73,
    )

    count = await TemplateManagementService(
        repository,
        FakeVendor(check_type=2, remark="资质已撤销"),
    ).sync_pending(template_id=1)

    assert count == 1
    assert repository.states == [(1, "21", 73, "rejected", "资质已撤销")]


@pytest.mark.asyncio
async def test_non_rejected_vendor_state_clears_stale_reason() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        "21",
        "approved",
        "历史驳回原因",
    )

    count = await TemplateManagementService(
        repository,
        FakeVendor(check_type=1, remark="厂商仍返回历史说明"),
    ).sync_pending()

    assert count == 1
    assert repository.states == [(1, "21", 0, "approved", None)]


@pytest.mark.asyncio
async def test_rejected_vendor_reason_is_limited_to_database_contract() -> None:
    repository = FakeRepository()

    await TemplateManagementService(
        repository,
        FakeVendor(check_type=2, remark="过" * 300),
    ).sync_pending()

    assert repository.states == [(1, "21", 0, "rejected", "过" * 256)]


@pytest.mark.asyncio
async def test_sync_skips_unchanged_vendor_state_and_reason() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "验证码",
        "验证码{1}",
        [{"pos": 1, "max_len": 6}],
        "平台部",
        "21",
        "rejected",
        "材料不足",
    )
    vendor = FakeVendor(check_type=2, remark="材料不足")

    count = await TemplateManagementService(repository, vendor).sync_pending()

    assert count == 0
    assert vendor.queried == [[21]]
    assert repository.states == []


@pytest.mark.parametrize(
    ("response", "error"),
    (
        ([], "incomplete"),
        ([{"id": 22, "checkType": 1, "checkRemark": None}], "unexpected"),
        (
            [
                {"id": 21, "checkType": 1, "checkRemark": None},
                {"id": 21, "checkType": 1, "checkRemark": None},
            ],
            "duplicate",
        ),
        ([{"id": 21, "checkType": 9, "checkRemark": None}], "unknown"),
        ([{"id": True, "checkType": 1, "checkRemark": None}], "invalid"),
    ),
)
@pytest.mark.asyncio
async def test_sync_rejects_invalid_or_incomplete_vendor_batch_before_write(
    response: list[dict[str, Any]],
    error: str,
) -> None:
    repository = FakeRepository()

    with pytest.raises(ValueError, match=error):
        await TemplateManagementService(
            repository,
            FakeVendor(response=response),
        ).sync_pending()

    assert repository.states == []


@pytest.mark.asyncio
async def test_sync_rejects_duplicate_local_vendor_binding_before_call() -> None:
    repository = FakeRepository()
    repository.records[2] = TemplateRecord(
        2,
        "重复绑定",
        "通知",
        [],
        "平台部",
        "021",
        "approved",
        None,
    )
    vendor = FakeVendor()

    with pytest.raises(ValueError, match="duplicate local"):
        await TemplateManagementService(repository, vendor).sync_pending()

    assert vendor.queried == []


@pytest.mark.asyncio
async def test_sync_maps_reordered_complete_vendor_batch_by_id() -> None:
    repository = FakeRepository()
    repository.records[2] = TemplateRecord(
        2,
        "通知",
        "系统通知",
        [],
        "平台部",
        "22",
        "approved",
        None,
        8,
    )
    vendor = FakeVendor(
        response=[
            {"id": 22, "checkType": 2, "checkRemark": "资质撤销"},
            {"id": 21, "checkType": 1, "checkRemark": None},
        ]
    )

    assert await TemplateManagementService(repository, vendor).sync_pending() == 2
    assert vendor.queried == [[21, 22]]
    assert repository.states == [
        (2, "22", 8, "rejected", "资质撤销"),
        (1, "21", 0, "approved", None),
    ]


@pytest.mark.asyncio
async def test_bound_rejected_template_must_be_recreated() -> None:
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
    with pytest.raises(TemplateStateConflict, match="已绑定厂商编号"):
        await TemplateManagementService(repository, FakeVendor()).update(
            1,
            name="新模板",
            content="新{1}",
            var_specs=[{"pos": 1, "max_len": 8}],
            actor="operator01",
        )


@pytest.mark.asyncio
async def test_unbound_draft_template_can_be_updated_and_submitted() -> None:
    repository = FakeRepository()
    repository.records[1] = TemplateRecord(
        1,
        "草稿",
        "旧{1}",
        [{"pos": 1, "max_len": 2}],
        "平台部",
        None,
        "draft",
        None,
    )

    updated = await TemplateManagementService(repository, FakeVendor()).update(
        1,
        name="新模板",
        content="新{1}",
        var_specs=[{"pos": 1, "max_len": 8}],
        actor="operator01",
    )

    assert updated.vendor_state == "pending"
    assert updated.vendor_template_id is None


@pytest.mark.asyncio
async def test_delete_removes_unreferenced_nonapproved_template() -> None:
    repository = FakeRepository()
    current = repository.records[1]
    repository.records[1] = TemplateRecord(
        current.id,
        current.name,
        current.content,
        current.var_specs,
        current.dept,
        None,
        current.vendor_state,
        current.vendor_reject_reason,
    )
    await TemplateManagementService(repository, FakeVendor()).delete(1, actor="operator01")
    assert repository.records == {}
