"""签名 CRUD、厂商提交与审核状态同步编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.sensitive_text import mask_phone_in_text
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
    async def create(self, *, name: str, actor: str) -> SignRecord: ...
    async def update(
        self, sign_id: int, *, name: str, actor: str
    ) -> SignRecord | None: ...
    async def delete(self, sign_id: int, *, actor: str) -> bool: ...
    async def pending(self, sign_id: int | None = None) -> list[SignRecord]: ...
    async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int: ...
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
    try:
        return {0: "pending", 1: "approved", 2: "rejected"}[check_type]
    except KeyError:
        raise ValueError(f"unknown sign checkType: {check_type}") from None


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
        response = await self._vendor().get_sign_state(list(vendor_to_local))
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
                states.append(
                    (local_id, map_vendor_sign_state(check_type), mask_phone_in_text(reason))
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
        if len(response) != 1:
            raise ValueError("invalid GetSignState adoption response")
        item = response[0]
        returned_id = item.get("id")
        check_type = item.get("checkType")
        reason = item.get("checkRemark")
        if (
            returned_id != vendor_sign_id
            or isinstance(returned_id, bool)
            or not isinstance(check_type, int)
            or isinstance(check_type, bool)
            or (reason is not None and not isinstance(reason, str))
        ):
            raise ValueError("invalid GetSignState adoption item")
        state = map_vendor_sign_state(check_type)
        applied = await self.repository.adopt_existing(
            sign_id,
            expected_vendor_id,
            state,
            mask_phone_in_text(reason),
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
        vendor_id = await self._vendor().bind_sign(format_sign_name(record.name))
        return int(await self.repository.apply_binding(sign_id, str(vendor_id)))
