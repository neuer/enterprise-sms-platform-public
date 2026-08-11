"""历史厂商事件重复检测；只输出无明文、无 HMAC 的修复候选。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.content_protection import decrypt_reply_content
from app.services.crypto import CryptoService
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class LegacyReplyFact:
    event_key: str
    vendor_task_id: str
    custom_id: str | None
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    content: str
    reply_time: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DuplicateReplyGroup:
    group_key: str
    keep_event_key: str
    duplicate_event_keys: tuple[str, ...]
    phone_masks: tuple[str, ...]

    def safe_json(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "keep_event_key": self.keep_event_key,
            "duplicate_event_keys": list(self.duplicate_event_keys),
            "phone_masks": list(self.phone_masks),
            "duplicate_projection_count": len(self.duplicate_event_keys),
        }


def _legacy_reply_fingerprint(
    fact: LegacyReplyFact,
    crypto: CryptoService,
) -> str:
    """受控解密后立即转换为最老保留 HMAC，不返回手机号或 digest。"""

    phone = crypto.decrypt_phone(
        fact.phone_enc,
        fact.key_version,
        fact.phone_hmac,
        table="reply_event",
    )
    candidates = crypto.hmac_candidates(phone)
    timestamp = fact.reply_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    values = (
        fact.vendor_task_id,
        fact.custom_id or "",
        candidates[min(candidates)],
        fact.content,
        timestamp,
    )
    canonical = "".join(f"{len(value.encode('utf-8'))}:{value}" for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find_legacy_reply_duplicates(
    facts: list[LegacyReplyFact],
    crypto: CryptoService,
) -> list[DuplicateReplyGroup]:
    """跨 HMAC 版本归并历史回复，只返回可审核的投影事件键。"""

    grouped: dict[str, list[LegacyReplyFact]] = {}
    for fact in facts:
        grouped.setdefault(_legacy_reply_fingerprint(fact, crypto), []).append(fact)
    duplicates: list[DuplicateReplyGroup] = []
    for group_key, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda item: (item.created_at, item.event_key))
        duplicates.append(
            DuplicateReplyGroup(
                group_key=group_key,
                keep_event_key=ordered[0].event_key,
                duplicate_event_keys=tuple(item.event_key for item in ordered[1:]),
                phone_masks=tuple(sorted({item.phone_mask for item in ordered})),
            )
        )
    return duplicates


class SqlVendorEventAuditRepository:
    """只读加载升级回填的 legacy reply facts。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def legacy_reply_facts(self, crypto: CryptoService) -> list[LegacyReplyFact]:
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT trim(event_key) event_key,vendor_task_id,custom_id,
                          phone_enc,trim(phone_hmac) phone_hmac,phone_mask,key_version,
                          content_enc,reply_time,created_at
                        FROM reply_event
                        WHERE raw_id IS NULL
                        ORDER BY created_at,event_key
                        """
                    )
                )
                return [
                    LegacyReplyFact(
                        event_key=str(row["event_key"]),
                        vendor_task_id=str(row["vendor_task_id"]),
                        custom_id=(str(row["custom_id"]) if row["custom_id"] is not None else None),
                        phone_enc=bytes(row["phone_enc"]),
                        phone_hmac=str(row["phone_hmac"]),
                        phone_mask=str(row["phone_mask"]),
                        key_version=int(row["key_version"]),
                        content=decrypt_reply_content(
                            crypto,
                            row["content_enc"],
                            str(row["event_key"]),
                        ),
                        reply_time=row["reply_time"],
                        created_at=row["created_at"],
                    )
                    for row in result.mappings()
                ]
        finally:
            await engine.dispose()

    async def duplicate_reply_groups(
        self,
        crypto: CryptoService,
    ) -> list[DuplicateReplyGroup]:
        return find_legacy_reply_duplicates(await self.legacy_reply_facts(crypto), crypto)
