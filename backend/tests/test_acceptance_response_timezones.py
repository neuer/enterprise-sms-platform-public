"""首次受理与数据库幂等重放使用相同的上海时间 JSON 合同。"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.api.messages import SendResponseModel
from app.api.web_messages import WebSendResponse
from app.services.pipeline_contracts import BatchResponse


@pytest.mark.parametrize("model", [SendResponseModel, WebSendResponse])
@pytest.mark.parametrize("idempotent", [False, True])
@pytest.mark.parametrize("offset", [0, -7 * 60, 5 * 60 + 30, 8 * 60])
def test_acceptance_and_replay_keep_instant_and_emit_shanghai_time(
    model: type[SendResponseModel] | type[WebSendResponse],
    idempotent: bool,
    offset: int,
) -> None:
    """输入时区、数据库 UTC 与首次/重放来源不影响调用方看到的格式。"""
    instant = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)
    scheduled = instant.astimezone(timezone(timedelta(minutes=offset)))
    result = BatchResponse(
        "test-batch",
        idempotent,
        1,
        0,
        0,
        0,
        1,
        1,
        "scheduled",
        None,
        scheduled,
        scheduled + timedelta(days=7),
    )
    data = model(**asdict(result)).model_dump(mode="json")
    assert data["scheduled_at"] == "2026-10-30T07:30:00+08:00"
    assert datetime.fromisoformat(data["scheduled_at"]) == instant
    if "idempotency_expires_at" in data:
        assert data["idempotency_expires_at"] == "2026-11-06T07:30:00+08:00"


@pytest.mark.parametrize("field", ["scheduled_at", "idempotency_expires_at"])
def test_acceptance_response_rejects_timezone_less_facts(field: str) -> None:
    values = dict(
        batch_no="test-batch",
        idempotent=True,
        accepted=1,
        removed_duplicate=0,
        removed_blacklist=0,
        removed_freq_limit=0,
        est_segments=1,
        quota_cost=1,
        status="scheduled",
        deferred_reason=None,
        scheduled_at=None,
        idempotency_expires_at=None,
    )
    values[field] = datetime(2026, 10, 30, 7, 30)  # noqa: DTZ001 - negative fixture
    with pytest.raises(ValueError, match="timezone"):
        BatchResponse(**values)


def test_immediate_acceptance_keeps_absent_times_null() -> None:
    result = BatchResponse("test-batch", False, 1, 0, 0, 0, 1, 1, "queued", None, None)
    assert result.scheduled_at is None
    assert result.idempotency_expires_at is None
