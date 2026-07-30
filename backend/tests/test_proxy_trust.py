from __future__ import annotations

from typing import cast

import pytest
from uvicorn._types import (
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendCallable,
    ASGISendEvent,
    Scope,
)
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


@pytest.mark.asyncio
async def test_untrusted_direct_client_cannot_spoof_forwarded_for() -> None:
    seen: list[str] = []

    async def app(
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        assert scope["type"] == "http"
        assert scope["client"] is not None
        seen.append(scope["client"][0])

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=["172.31.250.3"])

    async def receive() -> ASGIReceiveEvent:
        return {"type": "http.disconnect"}

    async def send(_message: ASGISendEvent) -> None:
        return None

    base = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"x-forwarded-for", b"192.0.2.4")],
    }
    await middleware(cast(Scope, {**base, "client": ("198.51.100.9", 1234)}), receive, send)
    await middleware(cast(Scope, {**base, "client": ("172.31.250.3", 1234)}), receive, send)

    assert seen == ["198.51.100.9", "192.0.2.4"]
