from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

import app.services.alert as alert_module
from app.services.alert import AlertEvent, WeComChannel

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"
EVENT = AlertEvent("job_stale", "crit", "任务异常", {"job": "poll"}, "job:poll")


class FakeResponseContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> httpx.Response:
        return self.response

    async def __aexit__(self, *_: object) -> None:
        await self.response.aclose()


class FakeClient:
    response: httpx.Response

    def __init__(self, **kwargs: object) -> None:
        assert isinstance(kwargs["timeout"], httpx.Timeout)
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def stream(self, method: str, url: str, **_: object) -> FakeResponseContext:
        request = httpx.Request(method, url)
        self.response.request = request
        return FakeResponseContext(self.response)


class SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(0.05)
        yield b'{"errcode":0}'


@pytest.mark.asyncio
async def test_wecom_rejects_declared_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeClient.response = httpx.Response(
        200,
        headers={"content-length": str(alert_module.WECOM_MAX_RESPONSE_BYTES + 1)},
        content=b"",
    )
    monkeypatch.setattr(alert_module.httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="too large"):
        await WeComChannel().send(WEBHOOK, EVENT)


@pytest.mark.asyncio
async def test_wecom_enforces_one_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeClient.response = httpx.Response(200, stream=SlowStream())
    monkeypatch.setattr(alert_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(alert_module, "WECOM_DEADLINE_S", 0.01)

    with pytest.raises(TimeoutError):
        await WeComChannel().send(WEBHOOK, EVENT)


@pytest.mark.asyncio
async def test_wecom_accepts_small_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"errcode": 0}).encode()
    FakeClient.response = httpx.Response(
        200,
        headers={"content-length": str(len(body))},
        content=body,
    )
    monkeypatch.setattr(alert_module.httpx, "AsyncClient", FakeClient)

    await WeComChannel().send(WEBHOOK, EVENT)
