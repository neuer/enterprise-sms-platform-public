"""审查整改的真实调用边界回归；仅使用合成主体和 MockTransport。"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services import alert as alerts
from app.services.idempotency import IdempotencyCoordinator, IdempotencyScope
from app.services.send_admission import SendAdmissionLimits
from app.services.send_admission_repository import SqlSendAdmissionRepository
from app.services.sign import format_sign_name
from app.tasks import log_task_failure
from app.tasks.send import ChunkPayload, SendWorker, SubmitOutcome
from tests.test_send_worker import FakeBucket, FakeGateway, FakeStore


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "status", "connection", "json", "length"])
async def test_wecom_boundary_never_logs_url_or_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    marker = "synthetic-credential-must-not-appear"
    webhook = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={marker}"

    def transport(request: httpx.Request) -> httpx.Response:
        if outcome == "connection":
            raise httpx.ConnectError(marker, request=request)
        if outcome == "length":
            return httpx.Response(200, content=b"{}", headers={"content-length": marker})
        if outcome == "json":
            return httpx.Response(200, content=marker.encode())
        return httpx.Response(503 if outcome == "status" else 200, json={"errcode": 0})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        alerts.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(transport),
        ),
    )
    caplog.set_level(logging.INFO)
    try:
        await alerts.WeComChannel().send(
            webhook, alerts.AlertEvent("synthetic", "warn", "合成告警", {"count": 1}, "synthetic")
        )
        assert outcome == "success"
    except RuntimeError as error:
        assert outcome != "success"
        assert marker not in "".join(traceback.format_exception(error))
        log_task_failure(
            task_id="synthetic",
            exception=error,
            traceback=error.__traceback__,
            sender=SimpleNamespace(name="synthetic", request=SimpleNamespace(correlation_id=None)),
        )
    assert marker not in caplog.text
    assert webhook not in caplog.text
    logging.getLogger("httpx").info("unrelated request")
    assert "unrelated request" in caplog.text


@pytest.mark.asyncio
async def test_recovery_budget_eval_failure_cannot_use_nonatomic_hash_fallback() -> None:
    class Redis:
        hash_calls = 0

        async def eval(self, *args: object) -> int:
            raise TimeoutError("synthetic uncertain EVAL")

        async def time(self) -> tuple[int, int]:
            self.hash_calls += 1
            return (1, 0)

        async def hget(self, *args: object) -> None:
            self.hash_calls += 1

        async def hset(self, *args: object, **kwargs: object) -> None:
            self.hash_calls += 1

        async def hmget(self, *args: object) -> list[int]:
            return [0, 0, 0]

        async def expire(self, *args: object) -> None:
            self.hash_calls += 1

    redis = Redis()
    repository = SqlSendAdmissionRepository(
        settings=SimpleNamespace(database_url="unused", redis_control_url="unused"), redis=redis
    )  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="unavailable"):
        await repository.consume_recovery_budget(
            epoch=1, batches=1, recipients=1, segments=1, limits=SendAdmissionLimits()
        )
    assert redis.hash_calls == 0


@pytest.mark.parametrize("value", ["199" + "0" * 7 + "1", "order-" + "199" + "0" * 7 + "1"])
def test_phone_business_identifiers_are_rejected_before_cache(value: str) -> None:
    scope = IdempotencyScope("app", "1")
    for helper in (
        IdempotencyCoordinator.key,
        IdempotencyCoordinator.claim_key,
        IdempotencyCoordinator.frequency_result_key,
    ):
        with pytest.raises(ValueError, match="手机号"):
            helper(scope, value)
    with pytest.raises(ValueError, match="手机号"):
        format_sign_name(value)
    assert (
        IdempotencyCoordinator.key(scope, "19900000001" + "a" * 21)
        == "idem:app:1:19900000001" + "a" * 21
    )
    assert format_sign_name("合成通知") == "【合成通知】"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_test", [False, True])
async def test_market_clock_is_checked_after_authorization_before_http(is_test: bool) -> None:
    times = iter(
        [
            datetime.fromisoformat(value)
            for value in [
                "2026-09-12T20:59:59+08:00",
                "2026-09-12T20:59:59+08:00",
                "2026-09-12T21:00:00+08:00",
            ]
        ]
    )
    gateway = FakeGateway(["accepted"])
    store = FakeStore()
    worker = SendWorker(gateway, store, FakeBucket(), clock=lambda: next(times))
    chunk = ChunkPayload(
        7,
        8,
        "synthetic",
        ("199" + "0" * 7 + "1",),
        "合成通知",
        "",
        "",
        category="market",
        is_test=is_test,
    )
    outcome = await worker.submit(chunk, lane="bulk")
    assert gateway.calls == (1 if is_test else 0)
    assert outcome == (SubmitOutcome.SUBMITTED if is_test else SubmitOutcome.PAUSED)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "order", "order-"])
async def test_phone_metadata_rejected_at_both_word_and_business_boundaries(prefix: str) -> None:
    from app.services.sensitive import SensitiveWordIndex, SensitiveWordManager
    from app.services.sensitive_repository import SqlSensitiveWordRepository
    from tests.test_sensitive import FakeRepository

    value = prefix + "199" + "0" * 7 + "1"
    repository = FakeRepository()
    manager = SensitiveWordManager(repository, SensitiveWordIndex())
    with pytest.raises(ValueError, match="手机号"):
        await manager.add([value], actor="synthetic")
    database = SqlSensitiveWordRepository()
    database._engine = lambda: pytest.fail("invalid word reached database")
    with pytest.raises(ValueError, match="手机号"):
        await database.add_many([value], actor="synthetic")
    with pytest.raises(ValueError, match="手机号"):
        IdempotencyCoordinator.key(IdempotencyScope("app", "1"), value)
    result = await manager.add(["合成词条"], actor="synthetic")
    assert result.created[0].word == "合成词条"
