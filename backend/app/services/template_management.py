"""模板 CRUD、厂商提交与审核状态同步编排。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.services.template import VarSpecInput, to_vendor_template
from app.services.vendor_review import (
    map_vendor_review_state,
    persisted_vendor_id,
    returned_vendor_id,
    validated_vendor_reviews,
)


class TemplateNotFound(LookupError):
    pass


class TemplateStateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    id: int
    name: str
    content: str
    var_specs: list[dict[str, int]]
    dept: str
    vendor_template_id: str | None
    vendor_state: str
    vendor_reject_reason: str | None
    row_version: int = 0


TemplateStateUpdate = tuple[int, str, int, str, str | None]


class TemplateRepository(Protocol):
    async def create(
        self,
        *,
        name: str,
        content: str,
        var_specs: list[dict[str, int]],
        dept: str,
        actor: str,
    ) -> TemplateRecord: ...

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]: ...

    async def get(self, template_id: int, *, dept: str | None = None) -> TemplateRecord | None: ...

    async def update(
        self,
        template_id: int,
        *,
        name: str,
        content: str,
        var_specs: list[dict[str, int]],
        actor: str,
    ) -> TemplateRecord | None: ...

    async def delete(self, template_id: int, *, actor: str) -> bool: ...

    async def syncable(self, template_id: int | None = None) -> list[TemplateRecord]: ...

    async def apply_states(self, states: list[TemplateStateUpdate]) -> int: ...

    async def apply_binding(self, template_id: int, vendor_template_id: str) -> bool: ...


class TemplateVendor(Protocol):
    async def bind_template(self, template_content: str) -> int: ...

    async def get_template_state(self, template_ids: list[int]) -> list[dict[str, Any]]: ...


def map_vendor_template_state(check_type: int) -> str:
    return map_vendor_review_state(check_type, object_name="template")


def _normalized_specs(values: list[VarSpecInput]) -> list[dict[str, int]]:
    """借助唯一转换实现校验，并输出稳定 JSON 结构。"""

    normalized: list[dict[str, int]] = []
    for value in values:
        if isinstance(value, Mapping):
            normalized.append(
                {
                    "pos": cast(int, value["pos"]),
                    "max_len": cast(int, value["max_len"]),
                }
            )
        else:
            normalized.append({"pos": value.pos, "max_len": value.max_len})
    return normalized


class TemplateManagementService:
    def __init__(
        self,
        repository: TemplateRepository,
        vendor: TemplateVendor | None = None,
    ) -> None:
        self.repository = repository
        self.vendor = vendor

    def _vendor(self) -> TemplateVendor:
        if self.vendor is None:
            raise RuntimeError("vendor client is unavailable in this component")
        return self.vendor

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        # 保留参数以兼容既有端口，模板自此作为平台全局资源。
        del dept
        return await self.repository.list_all(dept=None)

    async def get(self, template_id: int, *, dept: str | None) -> TemplateRecord:
        # 部门不再参与模板可见性或权限判断。
        del dept
        record = await self.repository.get(template_id, dept=None)
        if record is None:
            raise TemplateNotFound("模板不存在")
        return record

    async def create(
        self,
        *,
        name: str,
        content: str,
        var_specs: list[VarSpecInput],
        dept: str,
        actor: str,
    ) -> TemplateRecord:
        to_vendor_template(content, var_specs)
        # dept 列暂留作数据库兼容字段，新模板统一写空值。
        del dept
        return await self.repository.create(
            name=name,
            content=content,
            var_specs=_normalized_specs(var_specs),
            dept="",
            actor=actor,
        )

    async def update(
        self,
        template_id: int,
        *,
        name: str,
        content: str,
        var_specs: list[VarSpecInput],
        actor: str,
    ) -> TemplateRecord:
        current = await self.repository.get(template_id)
        if current is None:
            raise TemplateNotFound("模板不存在")
        if current.vendor_state not in {"draft", "rejected"}:
            raise TemplateStateConflict("仅草稿或已拒绝模板可修改")
        to_vendor_template(content, var_specs)
        updated = await self.repository.update(
            template_id,
            name=name,
            content=content,
            var_specs=_normalized_specs(var_specs),
            actor=actor,
        )
        if updated is None:
            raise TemplateStateConflict(
                "模板已绑定厂商编号、状态已变化或已被发送批次引用，请新建模板"
            )
        return updated

    async def delete(self, template_id: int, *, actor: str) -> None:
        if not await self.repository.delete(template_id, actor=actor):
            raise TemplateStateConflict("模板已绑定厂商编号、已被引用或不存在")

    async def sync_pending(self, template_id: int | None = None) -> int:
        """同步所有已绑定模板，保留旧方法名以兼容任务入口。"""

        syncable = await self.repository.syncable(template_id)
        if template_id is not None and not syncable:
            if await self.repository.get(template_id) is None:
                raise TemplateNotFound("模板不存在")
            raise TemplateStateConflict("仅已有厂商编号的模板可同步")
        vendor_to_local: dict[int, TemplateRecord] = {}
        for item in syncable:
            if item.vendor_template_id is None:
                continue
            vendor_id = persisted_vendor_id(
                item.vendor_template_id,
                operation="template",
            )
            if vendor_id in vendor_to_local:
                raise ValueError("duplicate local template vendor id")
            vendor_to_local[vendor_id] = item
        if not vendor_to_local:
            return 0
        requested_ids = sorted(vendor_to_local)
        response = await self._vendor().get_template_state(requested_ids)
        reviews = validated_vendor_reviews(
            response,
            requested_ids,
            operation="GetTemplateState",
            object_name="template",
        )
        states: list[TemplateStateUpdate] = []
        for vendor_id, review in reviews.items():
            local = vendor_to_local[vendor_id]
            expected_vendor_template_id = local.vendor_template_id
            assert expected_vendor_template_id is not None
            if (
                review.state == local.vendor_state
                and review.reject_reason == local.vendor_reject_reason
            ):
                continue
            states.append(
                (
                    local.id,
                    expected_vendor_template_id,
                    local.row_version,
                    review.state,
                    review.reject_reason,
                )
            )
        return await self.repository.apply_states(states)

    async def bind(self, template_id: int) -> int:
        """仅凭据型 worker 可把持久化模板意图提交给厂商。"""

        record = await self.repository.get(template_id)
        if record is None or record.vendor_state != "pending":
            return 0
        if record.vendor_template_id is not None:
            return 0
        vendor_content = to_vendor_template(record.content, record.var_specs)
        vendor_id = returned_vendor_id(
            await self._vendor().bind_template(vendor_content),
            operation="template",
        )
        return int(await self.repository.apply_binding(template_id, str(vendor_id)))
