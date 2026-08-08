"""外部 canonical origin（scheme + hostname + effective port）的唯一来源。"""

from __future__ import annotations

from fastapi import Request


def canonical_origin(request: Request) -> tuple[str, str, int]:
    """返回经 Uvicorn 受信代理链规范化后的外部 origin 三元组。"""

    scheme = request.url.scheme
    hostname = (request.url.hostname or "").casefold()
    port = request.url.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port
