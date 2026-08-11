"""失败号码重发的最小解密边界与事实源读取。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import text

from app.core.auth.accounts import ActorPrincipal
from app.core.runtime_resources import database_engine
from app.services.batch_query import BatchAccessScope, BatchNotFound
from app.services.crypto import CryptoService, EncryptionContext
from app.services.pipeline import SendRequest
from app.settings import Settings, get_settings

RESEND_FAILED_BIZ_ID = "failed-recipients-v1"


class NoFailedRecipients(ValueError):
    """原批次没有可重发的 failed 明细。"""


@dataclass(frozen=True, slots=True)
class EncryptedFailedPhone:
    phone_enc: bytes
    phone_hmac: str
    key_version: int


@dataclass(frozen=True, slots=True)
class ResendSource:
    batch_no: str
    dept: str
    category: str
    channel: str
    send_content_enc: bytes
    sign_name: str | None
    consent_confirmed: bool
    is_test: bool
    failed_phones: tuple[EncryptedFailedPhone, ...]


class ResendRepository(Protocol):
    async def load(self, batch_no: str, scope: BatchAccessScope) -> ResendSource: ...


class SqlResendRepository:
    """只在受限 scope 内读取原批次和 failed 号码密文。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load(self, batch_no: str, scope: BatchAccessScope) -> ResendSource:
        predicate, scope_params = scope.sql()
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                batch_result = await connection.execute(
                    text(
                        f"""
                        SELECT trim(b.batch_no) AS batch_no,b.category,b.channel,
                               b.dept,b.send_content_enc,b.sign_name,
                               b.consent_confirmed,b.is_test
                        FROM sms_batch b
                        WHERE b.batch_no=:batch_no AND {predicate}
                        """
                    ),
                    {"batch_no": batch_no, **scope_params},
                )
                batch = batch_result.mappings().one_or_none()
                if batch is None:
                    raise BatchNotFound
                phone_result = await connection.execute(
                    text(
                        """
                        SELECT m.phone_enc,trim(m.phone_hmac) phone_hmac,m.key_version
                        FROM sms_message m
                        JOIN sms_batch b ON b.id=m.batch_id
                        WHERE b.batch_no=:batch_no AND m.status='failed'
                        ORDER BY m.id
                        """
                    ),
                    {"batch_no": batch_no},
                )
                phones = tuple(
                    EncryptedFailedPhone(
                        cast(bytes, row["phone_enc"]),
                        str(row["phone_hmac"]),
                        int(row["key_version"]),
                    )
                    for row in phone_result.mappings()
                )
                return ResendSource(
                    batch_no=str(batch["batch_no"]),
                    dept=str(batch["dept"]),
                    category=str(batch["category"]),
                    channel=str(batch["channel"]),
                    send_content_enc=cast(bytes, batch["send_content_enc"]),
                    sign_name=(str(batch["sign_name"]) if batch["sign_name"] is not None else None),
                    consent_confirmed=bool(batch["consent_confirmed"]),
                    is_test=bool(batch["is_test"]),
                    failed_phones=phones,
                )
        finally:
            await engine.dispose()


class ResendService:
    """在内存中解密 failed 号码并重建完整流水线请求。"""

    def __init__(self, repository: ResendRepository, crypto: CryptoService) -> None:
        self.repository = repository
        self.crypto = crypto

    async def build_request(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        *,
        actor: ActorPrincipal | None = None,
    ) -> SendRequest:
        source = await self.repository.load(batch_no, scope)
        if not source.failed_phones:
            raise NoFailedRecipients("原批次没有失败号码可重发")
        mobiles = tuple(
            self.crypto.decrypt_phone(item.phone_enc, item.key_version, item.phone_hmac)
            for item in source.failed_phones
        )
        content = self.crypto.decrypt_bound_packed_text(
            source.send_content_enc,
            EncryptionContext(
                domain="sms-content",
                table="sms_batch",
                column="send_content_enc",
                object_id=source.batch_no,
            ),
        )
        return SendRequest(
            category=source.category,
            mobiles=mobiles,
            content=content,
            sign_name=source.sign_name,
            channel=source.channel,
            consent_confirmed=source.consent_confirmed,
            actor=actor,
            biz_id=RESEND_FAILED_BIZ_ID,
            is_test=source.is_test,
            resend_of=source.batch_no,
            resend_dept=source.dept,
        )
