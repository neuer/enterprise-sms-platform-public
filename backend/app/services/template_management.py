"""模板 CRUD、厂商提交与审核状态同步编排。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.services.template import VarSpecInput, to_vendor_template


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


class TemplateRepository(Protocol):
    async def create(
        self,
        *,
        name: str,
        content: str,
        var_specs: list[dict[str, int]],
        dept: str,
        actor: str,
        vendor_template_id: str,
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
        vendor_template_id: str,
        actor: str,
    ) -> TemplateRecord | None: ...

    async def delete(self, template_id: int, *, actor: str) -> bool: ...

    async def pending(self, template_id: int | None = None) -> list[TemplateRecord]: ...

    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int: ...


class TemplateVendor(Protocol):
    async def bind_template(self, template_content: str) -> int: ...

    async def get_template_state(self, template_ids: list[int]) -> list[dict[str, Any]]: ...


def map_vendor_template_state(check_type: int) -> str:
    try:
        return {0: "pending", 1: "approved", 2: "rejected"}[check_type]
    except KeyError:
        raise ValueError(f"unknown template checkType: {check_type}") from None


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
    def __init__(self, repository: TemplateRepository, vendor: TemplateVendor) -> None:
        self.repository = repository
        self.vendor = vendor

    async def list_all(self, *, dept: str | None) -> list[TemplateRecord]:
        return await self.repository.list_all(dept=dept)

    async def get(self, template_id: int, *, dept: str | None) -> TemplateRecord:
        record = await self.repository.get(template_id, dept=dept)
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
        vendor_content = to_vendor_template(content, var_specs)
        vendor_id = await self.vendor.bind_template(vendor_content)
        return await self.repository.create(
            name=name,
            content=content,
            var_specs=_normalized_specs(var_specs),
            dept=dept,
            actor=actor,
            vendor_template_id=str(vendor_id),
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
        vendor_id = await self.vendor.bind_template(to_vendor_template(content, var_specs))
        updated = await self.repository.update(
            template_id,
            name=name,
            content=content,
            var_specs=_normalized_specs(var_specs),
            vendor_template_id=str(vendor_id),
            actor=actor,
        )
        if updated is None:
            raise TemplateStateConflict("模板状态已变化")
        return updated

    async def delete(self, template_id: int, *, actor: str) -> None:
        if not await self.repository.delete(template_id, actor=actor):
            raise TemplateStateConflict("模板已通过、已被引用或不存在")

    async def sync_pending(self, template_id: int | None = None) -> int:
        pending = await self.repository.pending(template_id)
        if template_id is not None and not pending:
            if await self.repository.get(template_id) is None:
                raise TemplateNotFound("模板不存在")
            raise TemplateStateConflict("仅待审核模板可同步")
        vendor_to_local = {
            int(item.vendor_template_id): item.id
            for item in pending
            if item.vendor_template_id is not None
        }
        if not vendor_to_local:
            return 0
        response = await self.vendor.get_template_state(list(vendor_to_local))
        states: list[tuple[int, str, str | None]] = []
        for item in response:
            vendor_id = item.get("id")
            check_type = item.get("checkType")
            remark = item.get("checkRemark")
            if (
                not isinstance(vendor_id, int)
                or isinstance(vendor_id, bool)
                or not isinstance(check_type, int)
                or isinstance(check_type, bool)
                or (remark is not None and not isinstance(remark, str))
            ):
                raise ValueError("invalid GetTemplateState item")
            local_id = vendor_to_local.get(vendor_id)
            if local_id is not None:
                states.append((local_id, map_vendor_template_state(check_type), remark))
        return await self.repository.apply_states(states)
