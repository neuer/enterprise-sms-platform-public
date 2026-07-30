"""签名 CRUD、厂商提交与审核状态同步编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.sign import format_sign_name


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


class SignRepository(Protocol):
    async def list_all(self) -> list[SignRecord]: ...
    async def get(self, sign_id: int) -> SignRecord | None: ...
    async def create(self, *, name: str, vendor_sign_id: str, actor: str) -> SignRecord: ...
    async def update(
        self, sign_id: int, *, name: str, vendor_sign_id: str, actor: str
    ) -> SignRecord | None: ...
    async def delete(self, sign_id: int, *, actor: str) -> bool: ...
    async def pending(self, sign_id: int | None = None) -> list[SignRecord]: ...
    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int: ...


class SignVendor(Protocol):
    async def bind_sign(self, sign_name: str) -> int: ...
    async def get_sign_state(self, sign_ids: list[int]) -> list[dict[str, Any]]: ...


def map_vendor_sign_state(check_type: int) -> str:
    try:
        return {0: "pending", 1: "approved", 2: "rejected"}[check_type]
    except KeyError:
        raise ValueError(f"unknown sign checkType: {check_type}") from None


class SignManagementService:
    def __init__(self, repository: SignRepository, vendor: SignVendor) -> None:
        self.repository = repository
        self.vendor = vendor

    async def list_all(self) -> list[SignRecord]:
        return await self.repository.list_all()

    async def get(self, sign_id: int) -> SignRecord:
        record = await self.repository.get(sign_id)
        if record is None:
            raise SignNotFound("签名不存在")
        return record

    async def create(self, *, name: str, actor: str) -> SignRecord:
        formatted = format_sign_name(name)
        vendor_id = await self.vendor.bind_sign(formatted)
        return await self.repository.create(
            name=formatted[1:-1], vendor_sign_id=str(vendor_id), actor=actor
        )

    async def update(self, sign_id: int, *, name: str, actor: str) -> SignRecord:
        current = await self.get(sign_id)
        if current.vendor_state != "rejected":
            raise SignStateConflict("仅已拒绝签名可修改")
        formatted = format_sign_name(name)
        vendor_id = await self.vendor.bind_sign(formatted)
        updated = await self.repository.update(
            sign_id,
            name=formatted[1:-1],
            vendor_sign_id=str(vendor_id),
            actor=actor,
        )
        if updated is None:
            raise SignStateConflict("签名状态已变化")
        return updated

    async def delete(self, sign_id: int, *, actor: str) -> None:
        if not await self.repository.delete(sign_id, actor=actor):
            raise SignStateConflict("签名已通过、已被引用或不存在")

    async def sync_pending(self, sign_id: int | None = None) -> int:
        pending = await self.repository.pending(sign_id)
        if sign_id is not None and not pending:
            if await self.repository.get(sign_id) is None:
                raise SignNotFound("签名不存在")
            raise SignStateConflict("仅待审核签名可同步")
        vendor_to_local = {
            int(item.vendor_sign_id): item.id
            for item in pending
            if item.vendor_sign_id is not None
        }
        if not vendor_to_local:
            return 0
        response = await self.vendor.get_sign_state(list(vendor_to_local))
        states: list[tuple[int, str, str | None]] = []
        for item in response:
            vendor_id = item.get("id")
            check_type = item.get("checkType")
            reason = item.get("checkRemark")
            if (
                not isinstance(vendor_id, int)
                or isinstance(vendor_id, bool)
                or not isinstance(check_type, int)
                or isinstance(check_type, bool)
                or (reason is not None and not isinstance(reason, str))
            ):
                raise ValueError("invalid GetSignState item")
            local_id = vendor_to_local.get(vendor_id)
            if local_id is not None:
                states.append((local_id, map_vendor_sign_state(check_type), reason))
        return await self.repository.apply_states(states)
