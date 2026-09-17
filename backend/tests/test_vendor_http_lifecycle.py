from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core import runtime_resources as resources
from app.tasks import send as send_module
from app.vendor import zhihui as http_pool
from app.vendor.zhihui import VendorResponseTooLarge, ZhihuiClient


class Settings:
    vendor_base_url = "https://vendor.example.test"
    vendor_live_test = False
    redis_control_url = "redis://unused"
    secrets = {"vendor_secret_name": "synthetic-name", "vendor_secret_key": "synthetic-v1"}

    def credential(self, name: str) -> str:
        return self.secrets[name]


@pytest.mark.asyncio
async def test_actual_send_components_reuse_http_and_reload_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []
    requests: list[dict[str, Any]] = []
    settings = Settings()
    settings.secrets = dict(settings.secrets)
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=httpx.ByteStream(b'{"code":0,"data":"task1"}'))

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["verify"] is True
        assert kwargs["limits"].max_connections == 4
        client = original(**kwargs, transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    async def stores() -> tuple[Any, Any, Any, int, int, int]:
        return settings, object(), SimpleNamespace(load_market_window=lambda: None), 500, 50, 10

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(send_module, "_store_components", stores)
    monkeypatch.setattr(send_module, "SqlAlertService", lambda _: object())
    try:
        for number in range(3):
            settings.secrets["vendor_secret_key"] = f"synthetic-v{number}"
            _worker, _store, gateway, _size = await send_module._components()
            await gateway.send(["13800138000"], "通知", custom_id=f"c{number}")
            await gateway.aclose()
        assert len(created) == 1
        assert not created[0].is_closed
        assert [request["secretKey"] for request in requests] == [
            "synthetic-v0",
            "synthetic-v1",
            "synthetic-v2",
        ]
        assert len(requests) == 3
        assert "synthetic" not in str(list(http_pool._CLIENTS))
    finally:
        await resources.close_runtime_resources()
    assert created[0].is_closed


@pytest.mark.asyncio
async def test_actual_prepare_creates_no_vendor_client_or_reads_vendor_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(redis_control_url="redis://unused")

    class Store:
        async def load_worker_config(self) -> tuple[int, int, int]:
            return 500, 50, 10

        async def prepare_chunks(self, batch_no: str, batch_size: int) -> tuple[list[int], str]:
            assert (batch_no, batch_size) == ("batch-1", 500)
            return [1, 2], "bulk"

    def forbidden(**_: object) -> None:
        raise AssertionError("prepare must not create an HTTP client")

    monkeypatch.setattr(send_module, "get_settings", lambda: settings)
    monkeypatch.setattr(send_module, "redis_client", lambda _: object())
    monkeypatch.setattr(send_module.CryptoService, "from_settings", lambda _: object())
    monkeypatch.setattr("app.tasks.send_repository.SqlChunkStore", lambda *_: Store())
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert await send_module._process_batch("batch-1") == 2


@pytest.mark.asyncio
async def test_invalid_credentials_do_not_allocate_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    settings.secrets = {"vendor_secret_name": "synthetic", "vendor_secret_key": ""}

    def forbidden(**_: object) -> None:
        raise AssertionError("invalid credentials must not allocate an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(ValueError, match="credentials"):
        ZhihuiClient.from_settings(settings, shared_http=True)


@pytest.mark.asyncio
async def test_shared_pool_configuration_and_fork_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get(url: str = "https://vendor.example.test", limit: int = 4096) -> httpx.AsyncClient:
        return http_pool.acquire_vendor_http_client(
            base_url=url, timeout_seconds=10, response_limits=(128, limit, 8192)
        )[0]

    first = get()
    second = get("https://other.example.test")
    third = get(limit=4097)
    assert first is get()
    assert len({id(first), id(second), id(third)}) == 3
    pid = resources.os.getpid()
    try:
        with monkeypatch.context() as context:
            context.setattr(resources.os, "getpid", lambda: pid + 1)
            inherited_replacement = get()
            assert inherited_replacement is not first
            # fork hook must discard inherited clients without closing parent transports.
            resources.discard_inherited_runtime_resources()
            assert not first.is_closed
            replacement = get()
            assert replacement is not inherited_replacement
            await resources.close_runtime_resources()
            assert replacement.is_closed
    finally:
        for client in (first, second, third, inherited_replacement):
            await client.aclose()
        await resources.close_runtime_resources()


def test_shared_client_never_crosses_event_loops() -> None:
    async def use_loop() -> httpx.AsyncClient:
        client, owned = http_pool.acquire_vendor_http_client(
            base_url="https://vendor.example.test",
            timeout_seconds=10,
            response_limits=(128, 4096, 8192),
        )
        assert not owned
        await asyncio.sleep(0)
        await resources.close_runtime_resources()
        return client

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(asyncio.run, use_loop()) for _ in range(2)]
        first, second = [future.result() for future in futures]
    assert first is not second
    assert first.is_closed and second.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_borrowed_client_keeps_pool_but_closes_failed_response(cancel: bool) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            yield b"oversized"

        async def aclose(self) -> None:
            closed.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ZhihuiClient(
            base_url="https://vendor.example.test",
            secret_name="synthetic",
            secret_key="synthetic",
            http_client=client,
            owns_http_client=False,
            max_response_body_bytes=2,
        )
        request = asyncio.create_task(gateway.get_balance())
        await entered.wait()
        if cancel:
            request.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else VendorResponseTooLarge):
            await request
        assert closed.is_set()
        await gateway.aclose()
        assert not client.is_closed


@pytest.mark.asyncio
async def test_configuration_cache_is_bounded_and_overflow_clients_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled request must not return")

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        client = original(**kwargs, transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    cached: list[ZhihuiClient] = []
    try:
        for index in range(http_pool.MAX_SHARED_VENDOR_CONTEXTS):
            settings = Settings()
            settings.vendor_base_url = f"https://vendor-{index}.example.test"
            cached.append(ZhihuiClient.from_settings(settings, shared_http=True))
        for index in range(8):
            settings = Settings()
            settings.vendor_base_url = f"https://overflow-{index}.example.test"
            gateway = ZhihuiClient.from_settings(settings, shared_http=True)
            assert len(http_pool._CLIENTS) == http_pool.MAX_SHARED_VENDOR_CONTEXTS
            request = asyncio.create_task(gateway.get_balance())
            await entered.wait()
            entered.clear()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await gateway.aclose()
            assert created[-1].is_closed
            assert all(not client.is_closed for client in created[: len(cached)])
    finally:
        await resources.close_runtime_resources()
    assert all(client.is_closed for client in created)
