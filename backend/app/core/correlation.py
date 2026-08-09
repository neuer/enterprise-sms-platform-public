"""HTTP、异步任务、Outbox 与外部回调共用的安全关联 ID。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import UUID, uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth.principal_context import audit_principal_scope

REQUEST_ID_HEADER = "X-Request-ID"
TASK_HEADER = "correlation_id"
_correlation_id: ContextVar[UUID | None] = ContextVar("correlation_id", default=None)


def parse_correlation_id(value: object) -> UUID | None:
    """只接受非 nil 的规范 UUID，拒绝任意日志字段注入。"""

    if not isinstance(value, (str, UUID)):
        return None
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (ValueError, AttributeError):
        return None
    if parsed.int == 0:
        return None
    return parsed


def current_correlation_id(*, create: bool = True) -> UUID | None:
    """读取当前关联 ID；无上下文的生产者可获得新的安全 UUID。"""

    current = _correlation_id.get()
    if current is not None or not create:
        return current
    return uuid4()


@contextmanager
def correlation_scope(value: UUID | str | None = None) -> Iterator[UUID]:
    """在当前执行上下文中绑定关联 ID，并保证离开时恢复原上下文。"""

    correlation_id = parse_correlation_id(value) or uuid4()
    token = _correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _correlation_id.reset(token)


def bind_correlation_id(value: object) -> tuple[UUID, Token[UUID | None]]:
    """供 Celery signal 在任务生命周期内绑定上下文。"""

    correlation_id = parse_correlation_id(value) or uuid4()
    return correlation_id, _correlation_id.set(correlation_id)


def reset_correlation_id(token: Token[UUID | None]) -> None:
    """恢复 Celery worker 的前一个上下文，避免 prefork 任务间串号。"""

    _correlation_id.reset(token)


def correlation_headers() -> dict[str, str]:
    """生成 Celery 消息头；不得包含请求正文、凭据或主体快照。"""

    correlation_id = current_correlation_id()
    assert correlation_id is not None
    return {TASK_HEADER: str(correlation_id)}


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求建立关联上下文，并在响应头回传同一 ID。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requested = parse_correlation_id(request.headers.get(REQUEST_ID_HEADER))
        with audit_principal_scope(), correlation_scope(requested) as correlation_id:
            request.state.correlation_id = correlation_id
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = str(correlation_id)
            return response
