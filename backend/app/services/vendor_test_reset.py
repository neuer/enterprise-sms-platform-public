"""真实联调配置重置在数据库侧的幂等终结器。"""

from __future__ import annotations

from typing import Protocol

from app.services.vendor_test_operation import VendorTestOperation


class VendorTestRecipientPurger(Protocol):
    async def purge_all(self, *, actor: str) -> int: ...


class VendorTestResetFinalizer:
    """凭据 agent 成功后清理加密号码，不生成公开结果投影。"""

    def __init__(self, repository: VendorTestRecipientPurger) -> None:
        self.repository = repository

    async def finalize(self, record: VendorTestOperation) -> None:
        await self.repository.purge_all(actor=record.actor)
