"""签名 CRUD、厂商提交与审核状态同步编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.sign import format_sign_name
from app.services.vendor_review import (
    map_vendor_review_state,
    persisted_vendor_id,
    returned_vendor_id,
    validated_vendor_reviews,
)


class SignNotFound(LookupError):
    pass


class SignStateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SignRecord:
    id: int
    name: str
    vendor_sign_id: str | None
    vendor_state: str
    vendor_reject_reason: str | None
    row_version: int = 0


SignStateUpdate = tuple[int, str, int, str, str | None]


class SignRepository(Protocol):
    async def list_all(self) -> list[SignRecord]: ...
    async def get(self, sign_id: int) -> SignRecord | None: ...
    async def create(self, *, name: str, actor: str) -> SignRecord: ...
    async def update(
        self, sign_id: int, *, name: str, actor: str
    ) -> SignRecord | None: ...
    async def delete(self, sign_id: int, *, actor: str) -> bool: ...
    async def syncable(self, sign_id: int | None = None) -> list[SignRecord]: ...
    async def apply_states(self, states: list[SignStateUpdate]) -> int: ...
    async def apply_binding(self, sign_id: int, vendor_sign_id: str) -> bool: ...
    async def adopt_existing(
        self,
        sign_id: int,
        vendor_sign_id: str,
        vendor_state: str,
        vendor_reject_reason: str | None,
    ) -> bool: ...


class SignVendor(Protocol):
    async def bind_sign(self, sign_name: str) -> int: ...
    async def get_sign_state(self, sign_ids: list[int]) -> list[dict[str, Any]]: ...


def map_vendor_sign_state(check_type: int) -> str:
    return map_vendor_review_state(check_type, object_name="sign")


class SignManagementService:
    def __init__(self, repository: SignRepository, vendor: SignVendor | None = None) -> None:
        self.repository = repository
        self.vendor = vendor

    def _vendor(self) -> SignVendor:
        if self.vendor is None:
            raise RuntimeError("vendor client is unavailable in this component")
        return self.vendor

    async def list_all(self) -> list[SignRecord]:
        return await self.repository.list_all()

    async def get(self, sign_id: int) -> SignRecord:
        record = await self.repository.get(sign_id)
        if record is None:
            raise SignNotFound("签名不存在")
        return record

    async def create(self, *, name: str, actor: str) -> SignRecord:
        formatted = format_sign_name(name)
        return await self.repository.create(name=formatted[1:-1], actor=actor)

    async def update(self, sign_id: int, *, name: str, actor: str) -> SignRecord:
        current = await self.get(sign_id)
        if current.vendor_state != "rejected":
            raise SignStateConflict("仅已拒绝签名可修改")
        formatted = format_sign_name(name)
        updated = await self.repository.update(
            sign_id,
            name=formatted[1:-1],
            actor=actor,
        )
        if updated is None:
            raise SignStateConflict("签名状态已变化或已被应用/发送批次引用")
        return updated

    async def delete(self, sign_id: int, *, actor: str) -> None:
        if not await self.repository.delete(sign_id, actor=actor):
            raise SignStateConflict("签名已通过、已被引用或不存在")

    async def sync_pending(self, sign_id: int | None = None) -> int:
        """同步所有已绑定签名，保留旧方法名以兼容任务入口。"""

        syncable = await self.repository.syncable(sign_id)
        if sign_id is not None and not syncable:
            if await self.repository.get(sign_id) is None:
                raise SignNotFound("签名不存在")
            raise SignStateConflict("仅已有厂商编号的签名可同步")
        vendor_to_local: dict[int, SignRecord] = {}
        for item in syncable:
            if item.vendor_sign_id is None:
                continue
            vendor_id = persisted_vendor_id(item.vendor_sign_id, operation="sign")
            if vendor_id in vendor_to_local:
                raise ValueError("duplicate local sign vendor id")
            vendor_to_local[vendor_id] = item
        if not vendor_to_local:
            return 0
        requested_ids = sorted(vendor_to_local)
        response = await self._vendor().get_sign_state(requested_ids)
        reviews = validated_vendor_reviews(
            response,
            requested_ids,
            operation="GetSignState",
            object_name="sign",
        )
        states: list[SignStateUpdate] = []
        for vendor_id, review in reviews.items():
            local = vendor_to_local[vendor_id]
            expected_vendor_sign_id = local.vendor_sign_id
            assert expected_vendor_sign_id is not None
            if (
                review.state == local.vendor_state
                and review.reject_reason == local.vendor_reject_reason
            ):
                continue
            states.append(
                (
                    local.id,
                    expected_vendor_sign_id,
                    local.row_version,
                    review.state,
                    review.reject_reason,
                )
            )
        return await self.repository.apply_states(states)

    async def prepare_adoption(
        self,
        sign_id: int,
        *,
        confirmed_name: str,
    ) -> SignRecord:
        """确认管理员显式核对的是当前待绑定签名。"""

        record = await self.get(sign_id)
        if record.vendor_state != "pending" or record.vendor_sign_id is not None:
            raise SignStateConflict("仅待审核且尚未绑定厂商编号的签名可关联")
        if confirmed_name != record.name:
            raise SignStateConflict("确认的签名名称与当前记录不一致")
        return record

    async def adopt_existing(self, sign_id: int, vendor_sign_id: int) -> int:
        """由凭据型 worker 查询精确厂商 ID 后原子采用其真实状态。"""

        if (
            isinstance(vendor_sign_id, bool)
            or vendor_sign_id <= 0
            or vendor_sign_id > 2_147_483_647
        ):
            raise ValueError("invalid vendor sign id")
        record = await self.repository.get(sign_id)
        if record is None:
            return 0
        expected_vendor_id = str(vendor_sign_id)
        if record.vendor_sign_id is not None:
            if record.vendor_sign_id == expected_vendor_id:
                return 0
            raise SignStateConflict("签名已绑定其他厂商编号")
        if record.vendor_state != "pending":
            return 0

        response = await self._vendor().get_sign_state([vendor_sign_id])
        review = validated_vendor_reviews(
            response,
            [vendor_sign_id],
            operation="GetSignState",
            object_name="sign",
        )[vendor_sign_id]
        applied = await self.repository.adopt_existing(
            sign_id,
            expected_vendor_id,
            review.state,
            review.reject_reason,
        )
        if applied:
            return 1
        current = await self.repository.get(sign_id)
        if current is not None and current.vendor_sign_id == expected_vendor_id:
            return 0
        raise SignStateConflict("签名状态或厂商编号已变化")

    async def bind(self, sign_id: int) -> int:
        """仅凭据型 worker 可把持久化签名意图提交给厂商。"""

        record = await self.repository.get(sign_id)
        if record is None or record.vendor_state != "pending":
            return 0
        if record.vendor_sign_id is not None:
            return 0
        vendor_id = returned_vendor_id(
            await self._vendor().bind_sign(format_sign_name(record.name)),
            operation="sign",
        )
        return int(await self.repository.apply_binding(sign_id, str(vendor_id)))
