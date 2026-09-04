"""请求体字节上限；必须与 deploy/nginx.conf 的 client_max_body_size 对齐。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Literal

API_JSON_BODY_LIMIT = 1 * 1024 * 1024
IMPORT_BODY_LIMIT = 12 * 1024 * 1024
HEALTH_BODY_LIMIT = 1024
IMPORT_PATH = "/api/v1/web/messages/import"
HEALTH_PATHS = frozenset({"/livez", "/readyz", "/healthz"})
NO_BODY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

BodyPolicy = Literal["no_body", "json_1mib", "import_12mib"]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def body_policy_for(path: str, method: str) -> BodyPolicy:
    """未声明的读路由默认禁止 body；导入放宽不得被其他路由继承。"""

    selected = method.upper()
    if path in HEALTH_PATHS or selected in NO_BODY_METHODS or selected == "DELETE":
        return "no_body"
    if path == IMPORT_PATH and selected == "POST":
        return "import_12mib"
    return "json_1mib"


def body_limit_for(path: str, method: str) -> int:
    """按精确路径与方法选择上限；NO_BODY 路由上限为 0。"""

    policy = body_policy_for(path, method)
    if policy == "no_body":
        return 0
    if policy == "import_12mib":
        return IMPORT_BODY_LIMIT
    return API_JSON_BODY_LIMIT


class RequestBodyTooLarge(RuntimeError):
    """实际累计字节或声明长度超过路径上限。"""


class RequestBodyNotAllowed(RuntimeError):
    """NO_BODY 路由收到了请求体。"""


async def _send_error(send: Send, *, status: int, code: str, message: str, method: str) -> None:
    if method == "HEAD":
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", b"0"),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    payload = json.dumps(
        {"code": code, "message": message, "detail": None},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_payload_too_large(send: Send, method: str = "POST") -> None:
    await _send_error(
        send,
        status=413,
        code="PAYLOAD_TOO_LARGE",
        message="请求体超过上限",
        method=method,
    )


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
        transfer = headers.get("transfer-encoding", "")
        if limit == 0:
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    length = 1
                if length > 0:
                    await _send_error(
                        send,
                        status=400,
                        code="INVALID_PARAM",
                        message="该路由不允许请求体",
                        method=method,
                    )
                    return
            if "chunked" in transfer.casefold():
                await _send_error(
                    send,
                    status=400,
                    code="INVALID_PARAM",
                    message="该路由不允许请求体",
                    method=method,
                )
                return

            peeked: MutableMapping[str, Any] | None = None

            async def gated_receive() -> MutableMapping[str, Any]:
                nonlocal peeked
                if peeked is not None:
                    message = peeked
                    peeked = None
                    return message
                message = await receive()
                if message["type"] == "http.request" and (message.get("body") or b""):
                    raise RequestBodyNotAllowed
                return message

            started = False

            async def gated_send(message: MutableMapping[str, Any]) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            first = await receive()
            if first["type"] == "http.request" and (first.get("body") or b""):
                await _send_error(
                    send,
                    status=400,
                    code="INVALID_PARAM",
                    message="该路由不允许请求体",
                    method=method,
                )
                return
            peeked = first
            try:
                await self.app(scope, gated_receive, gated_send)
            except RequestBodyNotAllowed:
                if not started:
                    await _send_error(
                        send,
                        status=400,
                        code="INVALID_PARAM",
                        message="该路由不允许请求体",
                        method=method,
                    )
            return

        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = 0
            if length > limit:
                await _send_payload_too_large(send, method)
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
                await _send_payload_too_large(send, method)
