from __future__ import annotations

import base64
import inspect

import pytest

from app.services.batch_query import BatchAccessScope
from app.services.crypto import CryptoService, EncryptionContext
from app.services.pipeline_repository import SqlPipelineStore
from app.services.resend import (
    EncryptedFailedPhone,
    NoFailedRecipients,
    ResendService,
    ResendSource,
)


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def bound_content(service: CryptoService, batch_no: str, content: str) -> bytes:
    return service.encrypt_bound_packed_text(
        content,
        EncryptionContext(
            domain="sms-content",
            table="sms_batch",
            column="send_content_enc",
            object_id=batch_no,
        ),
    )


class FakeRepository:
    def __init__(self, source: ResendSource) -> None:
        self.source = source
        self.calls: list[tuple[str, BatchAccessScope]] = []

    async def load(self, batch_no: str, scope: BatchAccessScope) -> ResendSource:
        self.calls.append((batch_no, scope))
        return self.source


@pytest.mark.asyncio
async def test_resend_decrypts_failed_phones_in_memory_and_preserves_controls() -> None:
    service = crypto()
    protected = service.protect_phone("13800138000")
    repository = FakeRepository(
        ResendSource(
            batch_no="original-1",
            dept="市场部",
            category="market",
            channel="web",
            send_content_enc=bound_content(service, "original-1", "活动回T退订"),
            sign_name="【青鸾】",
            consent_confirmed=True,
            is_test=False,
            failed_phones=(
                EncryptedFailedPhone(
                    protected.phone_enc,
                    protected.phone_hmac,
                    protected.key_version,
                ),
            ),
        )
    )

    request = await ResendService(repository, service).build_request(
        "original-1",
        BatchAccessScope(dept="市场部"),
        actor="operator01",
    )

    assert request.mobiles == ("13800138000",)
    assert request.content == "活动回T退订"
    assert request.category == "market" and request.channel == "web"
    assert request.sign_name == "【青鸾】"
    assert request.consent_confirmed is True
    assert request.actor == "operator01"
    assert request.resend_of == "original-1"
    assert request.resend_dept == "市场部"
    assert request.biz_id is None and request.scheduled_at is None


@pytest.mark.asyncio
async def test_resend_rejects_source_without_failed_recipient() -> None:
    service = crypto()
    repository = FakeRepository(
        ResendSource(
            batch_no="original-1",
            dept="平台部",
            category="notice",
            channel="api",
            send_content_enc=bound_content(service, "original-1", "通知"),
            sign_name=None,
            consent_confirmed=False,
            is_test=False,
            failed_phones=(),
        )
    )
    with pytest.raises(NoFailedRecipients):
        await ResendService(repository, service).build_request(
            "original-1", BatchAccessScope(app_id=7)
        )


def test_optional_resend_reference_has_explicit_asyncpg_type() -> None:
    source = inspect.getsource(SqlPipelineStore._insert)
    assert "batch_no=CAST(:resend_of AS char(32))" in source
    assert ":resend_of IS NULL" not in source
