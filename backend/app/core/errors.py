"""统一平台 API 错误与 FastAPI 响应。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.correlation import REQUEST_ID_HEADER, current_correlation_id

LOGGER = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
    """输出固定 `{code,message,detail}`，不泄露内部异常。"""

    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message, "detail": error.detail},
    )


async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
    """把框架参数错误统一为平台 INVALID_PARAM，不回显输入值。"""

    fields = [".".join(str(part) for part in item["loc"]) for item in error.errors()]
    return JSONResponse(
        status_code=400,
        content={
            "code": "INVALID_PARAM",
            "message": "请求参数不合法",
            "detail": {"fields": fields},
        },
    )


async def internal_error_handler(request: Request, error: Exception) -> JSONResponse:
    """记录结构化安全上下文与完整堆栈，客户端只获得关联 ID。"""

    correlation_id = getattr(request.state, "correlation_id", None)
    correlation_id = correlation_id or current_correlation_id()
    app_context = getattr(request.state, "sms_app", None)
    LOGGER.error(
        "unhandled_http_exception",
        extra={
            "correlation_id": str(correlation_id),
            "http_method": request.method,
            "http_path": request.url.path,
            "actor_subject_kind": "api_app" if app_context is not None else None,
            "actor_app_id": getattr(app_context, "app_id", None),
            "error_type": type(error).__name__,
        },
        exc_info=(type(error), error, error.__traceback__),
    )

    return JSONResponse(
        status_code=500,
        headers={REQUEST_ID_HEADER: str(correlation_id)},
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务内部错误",
            "detail": {"request_id": str(correlation_id)},
        },
    )
