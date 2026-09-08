"""源应用策略必须通过实际内部加载路径进入发送流水线。"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.apikey import load_app_send_policy
from app.services.pipeline import MarketApiBulkForbidden, PipelineConfig, SendPipeline, SendRequest
from app.services.uncertain_resolution import (
    UncertainResolutionConflict,
    _load_resend_context,
    _load_source_api_app,
    _row,
)
from tests.test_send_pipeline import (
    FakeFrequency,
    FakeIdempotency,
    FakePublisher,
    FakeQuota,
    FakeStore,
    crypto,
)
from tests.test_uncertain_resolution import FakeConnection, FakeResult, _resolution


def source_policy(**changes: Any) -> dict[str, Any]:
    return {
        "id": 7, "name": "source", "dept": "current-dept", "status": 1,
        "allowed_categories": "notice,verify,market", "default_sign": None,
        "daily_quota": 100, "blacklist_check": True,
        "freq_override": {"verify_per_minute": 2, "verify_per_day": 3, "market_per_day": 4},
        "rate_limit_per_min": 8, "recipient_limit_per_min": 2, "segment_limit_per_min": 3,
        "max_in_flight_chunks": 9, "allow_market_api_bulk": True, "allowed_ips": None,
        "ip_allowlist_exempt_until": None, "unlimited_quota_exempt_until": None,
        **changes,
    }


async def source_context(**changes: Any) -> Any:
    current = _row({
        **_resolution(state="applying", action="resend_new_batch", confirmer=2),
        "source_channel": "api", "source_category": "verify", "source_dept": "approved-dept",
    })
    connection = FakeConnection([
        FakeResult(rows=[{"id": 1, "status": 1, "role": "admin"},
                         {"id": 2, "status": 1, "role": "admin"}]),
        FakeResult(source_policy(**changes)),
    ])
    context, app = await _load_resend_context(connection, current)
    assert app.dept == context.source_dept == "approved-dept"
    query = connection.calls[-1][0]
    assert "api_key" not in query and "SELECT *" not in query
    return app


@pytest.mark.asyncio
async def test_resend_policy_reaches_actual_pipeline_cost_and_frequency() -> None:
    class Limiter:
        def __init__(self) -> None:
            self.costs: list[dict[str, Any]] = []

        async def check(self, **values: Any) -> None:
            assert values["limit_per_minute"] == 8

        async def consume_send_cost(self, **values: Any) -> None:
            self.costs.append(values)

    app = await source_context()
    limiter = Limiter()
    frequency = FakeFrequency()
    pipeline = SendPipeline(
        store=FakeStore(), idempotency=FakeIdempotency(), crypto=crypto(),
        frequency=frequency, quota=FakeQuota(), publisher=FakePublisher(),
        config=PipelineConfig(), acceptance_limiter=limiter,  # type: ignore[arg-type]
    )
    result = await pipeline.accept(app, SendRequest("verify", ["13800138000"],
                                                   content="验证码123456", biz_id="r8-policy"))
    assert not result.idempotent
    assert limiter.costs[0]["recipient_limit"] == 2
    assert limiter.costs[0]["segment_limit"] == 3
    limits = frequency.values[0]["limits"]
    assert limits.verify_per_minute == 2
    assert limits.verify_per_day == 3
    assert limits.market_per_day == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_loaded_market_authorization_changes_real_gate(allowed: bool) -> None:
    app = await source_context(allow_market_api_bulk=allowed)
    pipeline = SendPipeline(
        store=FakeStore(), idempotency=FakeIdempotency(), crypto=crypto(),
        frequency=FakeFrequency(), quota=FakeQuota(), publisher=FakePublisher(),
        config=PipelineConfig(market_approval_threshold=2),
    )
    request = SendRequest("market", ["13800138000"], content="活动通知")
    if allowed:
        pipeline._enforce_market_api_bulk(app, request, 2)
    else:
        with pytest.raises(MarketApiBulkForbidden):
            pipeline._enforce_market_api_bulk(app, request, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", list(load_app_send_policy(source_policy())))
async def test_missing_policy_field_fails_closed(key: str) -> None:
    row = source_policy()
    row.pop("id" if key == "app_id" else key)
    with pytest.raises(UncertainResolutionConflict, match="发送策略不可用"):
        await _load_source_api_app(FakeConnection([FakeResult(row)]), 7, "verify")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"recipient_limit_per_min": 0}, {"segment_limit_per_min": True},
    {"freq_override": {"verify_per_minute": 0}},
    {"freq_override": {"unknown": 1}}, {"allow_market_api_bulk": "false"},
])
async def test_invalid_policy_does_not_widen_limits(changes: dict[str, Any]) -> None:
    with pytest.raises(UncertainResolutionConflict, match="发送策略不可用"):
        await source_context(**changes)


@pytest.mark.asyncio
async def test_valid_empty_policy_values_preserved_and_reload_observes_tightening() -> None:
    first = await source_context(freq_override=None, allow_market_api_bulk=False, daily_quota=0)
    second = await source_context(recipient_limit_per_min=1)
    assert first.freq_override is None and first.allow_market_api_bulk is False
    assert first.daily_quota == 0
    assert first.recipient_limit_per_min == 2 and second.recipient_limit_per_min == 1


@pytest.mark.asyncio
async def test_resend_keeps_frozen_unsigned_source_after_default_sign_changes() -> None:
    app = await source_context(default_sign="后来配置的签名")
    assert app.default_sign is None
