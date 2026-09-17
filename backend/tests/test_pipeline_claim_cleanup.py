"""Owner 取得后的拒绝、取消与清理异常必须收敛自己的租约。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.apikey import ApiAppContext
from app.services.app_ratelimit import ApplicationRateLimitExceeded
from app.services.pipeline import PipelineConfig, SendPipeline, SendRequest
from app.services.send_admission import SendAdmissionRejected
from tests.test_send_pipeline import (
    FakeFrequency,
    FakeIdempotency,
    FakePublisher,
    FakeQuota,
    FakeStore,
    crypto,
)


def pipeline(idem: Any, **kwargs: Any) -> SendPipeline:
    return SendPipeline(
        store=FakeStore(),
        idempotency=idem,
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
        **kwargs,
    )


APP = ApiAppContext(7, "app", "研发部", frozenset({"notice"}))
REQUEST = SendRequest("notice", ("13800138000",), content="通知", biz_id="cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "limit", "cancel", "create"])
async def test_pre_heartbeat_failure_releases_owner(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    idem = FakeIdempotency()
    service = pipeline(idem)
    error: BaseException
    if stage == "admission":
        error = SendAdmissionRejected("closed", "test", 30)
    elif stage == "limit":
        error = ApplicationRateLimitExceeded("rate limited")
    elif stage == "cancel":
        error = asyncio.CancelledError()
    else:
        error = RuntimeError("task creation failed")

    async def fail(*_args: Any) -> None:
        raise error

    if stage == "create":
        captured = []

        def fail_create(coro: Any) -> None:
            captured.append(coro)
            raise error

        monkeypatch.setattr(asyncio, "create_task", fail_create)
    else:
        monkeypatch.setattr(
            service, "_consume_request_limit" if stage == "limit" else "_authorize_new_send", fail
        )
    with pytest.raises(type(error)) as raised:
        await service.accept(APP, REQUEST)
    assert raised.value is error
    assert idem.released == ["claim-token"]
    if stage == "create":
        assert len(captured) == 1 and captured[0].cr_frame is None


@pytest.mark.asyncio
async def test_heartbeat_and_release_errors_preserve_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered = asyncio.Event()
    stopped = asyncio.Event()
    error = ValueError("original business error")

    class BrokenCleanup(FakeIdempotency):
        async def heartbeat(self, *_args: Any) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
                raise RuntimeError("sensitive heartbeat detail")

        async def release(self, *_args: Any) -> None:
            assert stopped.is_set()
            self.released.append("attempted")
            raise RuntimeError("sensitive release detail")

    idem = BrokenCleanup()
    service = pipeline(idem)

    async def business(*_args: Any, **_kwargs: Any) -> None:
        await entered.wait()
        raise error

    service._accept_claimed = business
    with pytest.raises(ValueError) as raised:
        await service.accept(APP, REQUEST)
    assert raised.value is error
    assert idem.released == ["attempted"]
    assert "heartbeat stop unavailable" in caplog.text
    assert "claim release unavailable" in caplog.text
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_during_release_waits_for_owned_cleanup() -> None:
    release_entered, finish = asyncio.Event(), asyncio.Event()

    class BlockingRelease(FakeIdempotency):
        async def release(self, *_args: Any) -> None:
            release_entered.set()
            await finish.wait()
            self.released.append("finished")

    idem = BlockingRelease()
    owner = asyncio.create_task(pipeline(idem).accept(APP, REQUEST))
    await release_entered.wait()
    owner.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert idem.released == ["finished"]
