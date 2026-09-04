"""请求体字节上限；必须与 deploy/nginx.conf 的 client_max_body_size 对齐。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

API_JSON_BODY_LIMIT = 1 * 1024 * 1024
IMPORT_BODY_LIMIT = 12 * 1024 * 1024
HEALTH_BODY_LIMIT = 1024
IMPORT_PATH = "/api/v1/web/messages/import"
HEALTH_PATHS = frozenset({"/livez", "/readyz", "/healthz"})

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def body_limit_for(path: str, method: str) -> int:
    """按精确路径与方法选择上限；导入放宽不得被其他路由继承。"""

    if path in HEALTH_PATHS:
        return HEALTH_BODY_LIMIT
    if path == IMPORT_PATH and method == "POST":
        return IMPORT_BODY_LIMIT
    if method in {"GET", "HEAD", "DELETE"}:
        return HEALTH_BODY_LIMIT
    return API_JSON_BODY_LIMIT


class RequestBodyTooLarge(RuntimeError):
    """实际累计字节或声明长度超过路径上限。"""


async def _send_payload_too_large(send: Send) -> None:
    payload = json.dumps(
        {
            "code": "PAYLOAD_TOO_LARGE",
            "message": "请求体超过上限",
            "detail": None,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestBodyLimitMiddleware:
    """在 JSON/表单解析前按实际字节切断；不信任单独的 Content-Length。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        limit = body_limit_for(path, method)
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        declared = headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = 0
            if length > limit:
                await _send_payload_too_large(send)
                return

        received = 0
        started = False

        async def limited_receive() -> MutableMapping[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not started:
                await _send_payload_too_large(send)
