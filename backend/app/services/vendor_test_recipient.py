"""真实联调测试号码的加密写入与安全投影。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.services.crypto import CryptoService

_LABEL_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InvalidVendorTestRecipient(ValueError):
    """测试号码或备注不符合合同，错误不携带原始输入。"""


class DuplicateVendorTestRecipient(ValueError):
    """任一可用 HMAC key 版本已登记该号码。"""


class RecipientNotFound(LookupError):
    """测试号码不存在或不是 active。"""


class RecipientBusy(RuntimeError):
    """仍有真实 UAT 操作时禁止停用或删除号码。"""


class RecipientHmacIndexStale(RuntimeError):
    """测试号码的 HMAC 别名未覆盖当前全部保留版本。"""


@dataclass(frozen=True, slots=True)
class VendorTestRecipientCreate:
    label: str
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int


@dataclass(frozen=True, slots=True)
class VendorTestRecipientRecord:
    id: int
    label: str
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    status: str
    created_by: str
    created_at: datetime
    disabled_by: str | None = None
    disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VendorTestRecipientSummary:
    id: int
    label: str
    phone_mask: str
    status: str
    created_at: datetime
    disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VendorTestRecipientForSend:
    id: int
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    hmac_candidates: tuple[tuple[int, str], ...]


class VendorTestRecipientRepository(Protocol):
    async def create(
        self,
        candidate: VendorTestRecipientCreate,
        candidates: dict[int, str],
        *,
        actor: str,
    ) -> VendorTestRecipientRecord: ...

    async def list_summaries(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[VendorTestRecipientSummary, ...]: ...

    async def disable(
        self,
        recipient_id: int,
        *,
        actor: str,
    ) -> VendorTestRecipientSummary: ...

    async def delete(self, recipient_id: int, *, actor: str) -> bool: ...

    async def purge_all(self, *, actor: str) -> int: ...

    async def resolve_for_send(self, recipient_id: int) -> VendorTestRecipientForSend: ...

    async def resolve_by_hmac_candidates(
        self,
        candidates: dict[int, str],
    ) -> VendorTestRecipientForSend: ...

    async def refresh_hmac_candidates(
        self,
        recipient_id: int,
        candidates: dict[int, str],
        *,
        actor: str,
    ) -> VendorTestRecipientSummary: ...


class VendorTestRecipientService:
    """统一保护号码；列表和页面永远只取得掩码投影。"""

    def __init__(
        self,
        repository: VendorTestRecipientRepository,
        crypto: CryptoService,
    ) -> None:
        self.repository = repository
        self.crypto = crypto

    @staticmethod
    def _label(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 64 or _LABEL_CONTROL.search(normalized):
            raise InvalidVendorTestRecipient("测试号码备注必须为 1-64 个可见字符")
        return normalized

    async def add(
        self,
        *,
        label: str,
        phone: str,
        actor: str,
    ) -> VendorTestRecipientRecord:
        normalized_label = self._label(label)
        try:
            protected = self.crypto.protect_phone(phone, table="vendor_test_recipient")
            candidates = self.crypto.hmac_candidates(phone)
        except ValueError:
            raise InvalidVendorTestRecipient("测试手机号格式无效") from None
        candidate = VendorTestRecipientCreate(
            label=normalized_label,
            phone_enc=protected.phone_enc,
            phone_hmac=protected.phone_hmac,
            phone_mask=protected.phone_mask,
            key_version=protected.key_version,
        )
        return await self.repository.create(candidate, candidates, actor=actor)

    async def list(
        self,
        *,
        include_disabled: bool = True,
    ) -> tuple[VendorTestRecipientSummary, ...]:
        return await self.repository.list_summaries(include_disabled=include_disabled)

    async def disable(
        self,
        recipient_id: int,
        *,
        actor: str,
    ) -> VendorTestRecipientSummary:
        return await self.repository.disable(recipient_id, actor=actor)

    async def delete(self, recipient_id: int, *, actor: str) -> bool:
        return await self.repository.delete(recipient_id, actor=actor)

    async def purge_all(self, *, actor: str) -> int:
        return await self.repository.purge_all(actor=actor)

    async def resolve_for_send(self, recipient_id: int) -> VendorTestRecipientForSend:
        recipient = await self.repository.resolve_for_send(recipient_id)
        return self._require_fresh_index(recipient)

    async def resolve_phone_for_send(self, phone: str) -> VendorTestRecipientForSend:
        """仅用不可逆 HMAC 候选定位 API UAT 号码，并返回受保护数据。"""

        try:
            candidates = self.crypto.hmac_candidates(phone)
        except ValueError:
            raise InvalidVendorTestRecipient("测试手机号格式无效") from None
        recipient = await self.repository.resolve_by_hmac_candidates(candidates)
        return self._require_fresh_index(
            recipient,
            expected_candidates=candidates,
        )

    def _require_fresh_index(
        self,
        recipient: VendorTestRecipientForSend,
        *,
        expected_candidates: dict[int, str] | None = None,
    ) -> VendorTestRecipientForSend:
        candidates = dict(recipient.hmac_candidates)
        if (
            len(candidates) != len(recipient.hmac_candidates)
            or set(candidates) != self.crypto.hmac_versions
            or candidates.get(recipient.key_version) != recipient.phone_hmac
            or (expected_candidates is not None and candidates != expected_candidates)
        ):
            raise RecipientHmacIndexStale("测试号码索引待刷新")
        return recipient

    async def refresh_hmac_index(
        self,
        recipient_id: int,
        *,
        phone: str,
        actor: str,
    ) -> VendorTestRecipientSummary:
        """用管理员当次重录的明文重建索引；不解密也不回显历史号码。"""

        try:
            candidates = self.crypto.hmac_candidates(phone)
        except ValueError:
            raise InvalidVendorTestRecipient("测试手机号格式无效") from None
        return await self.repository.refresh_hmac_candidates(
            recipient_id,
            candidates,
            actor=actor,
        )
