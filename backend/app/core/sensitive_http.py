"""敏感出站请求的第三方诊断日志隔离。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SENSITIVE_REQUEST: ContextVar[bool] = ContextVar("sensitive_http_request", default=False)


class _RequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENSITIVE_REQUEST.get()


# 锁定版本 HTTPX/httpcore 的 logger；父 logger 的 filter 不作用于子 logger。
for _name in (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(_name).addFilter(_RequestLogFilter())


@contextmanager
def sensitive_http_request() -> Iterator[None]:
    """只抑制当前请求的底层日志；业务分类日志和并发普通请求保持可观测。"""

    token = _SENSITIVE_REQUEST.set(True)
    try:
        yield
    finally:
        _SENSITIVE_REQUEST.reset(token)
