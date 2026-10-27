"""省略签名的幂等指纹不随当前默认签名变化。"""

from __future__ import annotations

import base64

import pytest

from app.core.apikey import ApiAppContext
from app.services.category import policy_for_category
from app.services.crypto import CryptoService
from app.services.idempotency import IdempotencyConflict, IdempotencyFingerprint
from app.services.pipeline import SendPipeline
from app.services.pipeline_contracts import SendRequest


def pipeline() -> SendPipeline:
    created = object.__new__(SendPipeline)
    key = base64.b64encode(b"k" * 32).decode()
    created.crypto = CryptoService.from_secret_values(key, key)
    return created


def test_omitted_sign_hash_stays_when_default_changes() -> None:
    subject = pipeline()
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="same")
    policy = policy_for_category("notice", frozenset({"notice"}))
    first = subject._request_hash(
        request,
        ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名甲"),
        policy,
    )
    second = subject._request_hash(
        request,
        ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名乙"),
        policy,
    )
    assert first == second


def test_explicit_sign_change_is_a_different_request() -> None:
    subject = pipeline()
    policy = policy_for_category("notice", frozenset({"notice"}))
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名甲")
    original = subject._request_hash(
        SendRequest("notice", ["13800138000"], content="通知", biz_id="same", sign_name="签名甲"),
        app,
        policy,
    )
    changed = subject._request_hash(
        SendRequest("notice", ["13800138000"], content="通知", biz_id="same", sign_name="签名乙"),
        app,
        policy,
    )
    assert original != changed


@pytest.mark.asyncio
async def test_legacy_omitted_sign_replays_frozen_batch_sign() -> None:
    subject = pipeline()
    policy = policy_for_category("notice", frozenset({"notice"}))
    original_app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名甲")
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="same")
    legacy = subject._request_hash(
        request,
        original_app,
        policy,
        format_version=1,
        frozen_sign="签名甲",
    )

    class Store:
        async def request_fingerprint(self, _scope, _biz_id):
            return IdempotencyFingerprint(legacy, subject.crypto.active_version, 1, "签名甲")

    subject.idempotency = Store()  # type: ignore[assignment]
    await subject._ensure_same_request(
        subject._idempotency_scope(request, original_app),
        "same",
        request,
        ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名乙"),
        policy,
    )


@pytest.mark.asyncio
async def test_legacy_without_frozen_sign_fails_closed() -> None:
    subject = pipeline()
    policy = policy_for_category("notice", frozenset({"notice"}))
    app = ApiAppContext(7, "app", "研发部", frozenset({"notice"}), default_sign="签名乙")
    request = SendRequest("notice", ["13800138000"], content="通知", biz_id="same")

    class Store:
        async def request_fingerprint(self, _scope, _biz_id):
            return IdempotencyFingerprint("a" * 64, subject.crypto.active_version, 1, None)

    subject.idempotency = Store()  # type: ignore[assignment]
    with pytest.raises(IdempotencyConflict, match="查询原批次"):
        await subject._ensure_same_request(
            subject._idempotency_scope(request, app),
            "same",
            request,
            app,
            policy,
        )
