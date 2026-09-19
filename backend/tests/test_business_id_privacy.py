"""业务标识必须在进入幂等存储前拒绝手机号，不能用 hex 外形豁免。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.messages import SendRequestModel, VendorTestApiUatRequestModel
from app.api.vendor_test import UatMessageRequestModel
from app.api.web_messages import WebSendRequest
from app.core.sensitive_text import reject_phone_business_id
from app.services.idempotency import IdempotencyCoordinator, IdempotencyScope
from app.services.vendor_test_operation_repository import SqlVendorTestOperationRepository

PHONE = "199" + "0" * 7 + "1"
UNSAFE_HEX = PHONE + "a" * 21
UNSAFE_IDS = [UNSAFE_HEX, "0" + PHONE + "a" * 20, PHONE + "0" + "a" * 20]
SAFE_HEX = "abcdefab-1234-4abc-8def-abcdefabcdef".replace("-", "")


@pytest.mark.parametrize(
    "value",
    [
        *UNSAFE_IDS,
        UNSAFE_HEX.upper(),
        "a" * 21 + PHONE,
        "abcdefab-1234-4abc-8def-a" + PHONE,
        "order-" + PHONE,
    ],
)
def test_business_id_has_no_format_exemption(value: str) -> None:
    with pytest.raises(ValueError, match="biz_id不得包含手机号") as failure:
        reject_phone_business_id(value, field_name="biz_id")
    assert value not in str(failure.value)


@pytest.mark.parametrize(
    "value", [None, "order-safe", SAFE_HEX, "abcdefab-1234-4abc-8def-abcdefabcdef"]
)
def test_non_phone_identifiers_remain_accepted(value: str | None) -> None:
    reject_phone_business_id(value, field_name="biz_id")


@pytest.mark.parametrize(
    "model,fields",
    [
        (SendRequestModel, {"mobiles": [PHONE]}),
        (VendorTestApiUatRequestModel, {"mobiles": [PHONE]}),
        (WebSendRequest, {"mobiles": [PHONE]}),
        (UatMessageRequestModel, {"recipient_id": 1, "app_id": 1}),
    ],
)
@pytest.mark.parametrize("unsafe_id", UNSAFE_IDS)
def test_all_send_models_reject_phone_hex_and_preserve_safe_id(model, fields, unsafe_id) -> None:
    payload = {"category": "notice", "content": "合成通知", **fields}
    with pytest.raises(ValidationError):
        model(**payload, biz_id=unsafe_id)
    assert model(**payload, biz_id=SAFE_HEX).biz_id == SAFE_HEX


@pytest.mark.asyncio
async def test_unsafe_id_cannot_reach_redis_or_uat_database() -> None:
    class Redis:
        async def set(self, *args, **kwargs):
            pytest.fail("unsafe identifier reached Redis")

    scope = IdempotencyScope("app", "1")
    coordinator = IdempotencyCoordinator(Redis(), SimpleNamespace())
    with pytest.raises(ValueError, match="手机号"):
        await coordinator.remember(scope, UNSAFE_HEX, "a" * 32)
    for helper in (coordinator.claim_key, coordinator.frequency_result_key):
        with pytest.raises(ValueError, match="手机号"):
            helper(scope, UNSAFE_HEX)
    with pytest.raises(ValueError, match="手机号"):
        coordinator.quota_result_key(scope, UNSAFE_HEX, "20260919")
    repository = SqlVendorTestOperationRepository()
    repository._engine = lambda: pytest.fail("unsafe identifier reached database")
    with pytest.raises(ValueError, match="手机号"):
        await repository.prepare_uat_acceptance(
            "abcdefab-1234-4abc-8def-abcdefabcdef",
            biz_id=UNSAFE_HEX,
            app_id=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["accept", "replay_if_present"])
@pytest.mark.parametrize("unsafe_id", UNSAFE_IDS)
async def test_direct_pipeline_rejects_before_idempotency_or_storage(
    operation: str, unsafe_id: str
) -> None:
    from app.core.apikey import ApiAppContext
    from app.services.pipeline import PipelineConfig, SendPipeline, SendRequest
    from tests.test_send_pipeline import (
        FakeFrequency,
        FakePublisher,
        FakeQuota,
        FakeStore,
        crypto,
    )

    class NoIdempotencyIO:
        def __getattr__(self, name: str):
            pytest.fail(f"unsafe identifier reached idempotency: {name}")

    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=NoIdempotencyIO(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    app = ApiAppContext(7, "synthetic", "synthetic", frozenset({"notice"}), "【通知】", 100)
    request = SendRequest("notice", [PHONE], content="合成通知", biz_id=unsafe_id)
    with pytest.raises(ValueError, match="手机号"):
        await getattr(pipeline, operation)(app, request)
    assert store.commands == []


@pytest.mark.parametrize("value", UNSAFE_IDS)
def test_management_metadata_rejects_adjacent_digit_padding(value: str) -> None:
    from app.core.sensitive_text import reject_phone_in_text
    from app.services.sign import format_sign_name

    with pytest.raises(ValueError, match="手机号"):
        reject_phone_in_text(value, field_name="敏感词")
    with pytest.raises(ValueError, match="手机号"):
        format_sign_name(value)
