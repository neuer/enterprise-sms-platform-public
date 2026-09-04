"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.admin import router as admin_router
from app.api.approvals import router as approvals_router
from app.api.apps import router as apps_router
from app.api.auth import router as auth_router
from app.api.auth_providers import router as auth_providers_router
from app.api.blacklist import router as blacklist_router
from app.api.callbacks import router as callbacks_router
from app.api.messages import router as messages_router
from app.api.metrics import router as metrics_router
from app.api.ops import router as ops_router
from app.api.replies import router as replies_router
from app.api.reports import router as reports_router
from app.api.security_daily import router as security_daily_router
from app.api.sensitive_words import router as sensitive_words_router
from app.api.signs import router as signs_router
from app.api.templates import router as templates_router
from app.api.users import router as users_router
from app.api.vendor_test import router as vendor_test_router
from app.api.web_messages import router as web_messages_router
from app.build_info import APP_VERSION
from app.core.bounded_executor import close_bounded_executor
from app.core.correlation import CorrelationIdMiddleware
from app.core.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    internal_error_handler,
    validation_error_handler,
)
from app.core.health import create_readiness_probe
from app.core.jobtrack import create_default_heartbeat_service
from app.core.request_limits import RequestBodyLimitMiddleware
from app.core.runtime_resources import (
    close_runtime_resources,
    configure_runtime_resources,
)
from app.core.runtime_telemetry import create_runtime_monitor
from app.settings import Settings, get_settings
from app.tasks import register_task_modules
from app.tasks.scheduler import load_and_apply_job_intervals

LOGGER = logging.getLogger(__name__)


class StartupConfigGate:
    """数据库恢复后只执行一次启动配置加载，成功前 readyz 保持失败。"""

    def __init__(self, loader: Callable[[], Awaitable[None]]) -> None:
        self.loader = loader
        self.loaded = False
        self.lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self.loaded:
            return
        async with self.lock:
            if self.loaded:
                return
            await self.loader()
            self.loaded = True


def create_lifespan(
    settings: Settings,
) -> Any:
    """构造使用唯一 Settings 来源的应用 lifespan。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在 API 进程内启动和关闭后台任务心跳巡检。"""

        selected = getattr(application.state, "settings", settings)
        configure_runtime_resources(selected, component="api")
        heartbeat = None
        runtime_monitor = None
        try:
            register_task_modules()
            startup_gate: StartupConfigGate = application.state.startup_config_gate
            try:
                await startup_gate.ensure()
            except Exception as error:
                LOGGER.warning(
                    "startup_configuration_not_ready",
                    extra={"error_type": type(error).__name__},
                )
            heartbeat = create_default_heartbeat_service()
            runtime_monitor = create_runtime_monitor()
            application.state.job_heartbeat = heartbeat
            application.state.runtime_monitor = runtime_monitor
            heartbeat.start()
            runtime_monitor.start()
            yield
        finally:
            if runtime_monitor is not None:
                await runtime_monitor.stop()
            if heartbeat is not None:
                await heartbeat.stop()
            await close_runtime_resources()
            close_bounded_executor()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建带版本元数据的 API 应用。"""

    selected_settings = settings or get_settings()
    documentation = None if selected_settings.is_production else "/docs"
    application = FastAPI(
        title="企业短信管理平台 API",
        version=APP_VERSION,
        lifespan=create_lifespan(selected_settings),
        docs_url=documentation,
        redoc_url=None if selected_settings.is_production else "/redoc",
        openapi_url=None if selected_settings.is_production else "/openapi.json",
    )
    startup_gate = StartupConfigGate(lambda: load_and_apply_job_intervals())
    application.state.settings = selected_settings
    application.state.startup_config_gate = startup_gate
    application.state.readiness_probe = create_readiness_probe(
        selected_settings,
        startup_check=startup_gate.ensure,
    )
    application.add_middleware(CorrelationIdMiddleware)
    application.add_middleware(RequestBodyLimitMiddleware)
    hosts = selected_settings.trusted_host_list
    if hosts != ["*"]:
        application.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        StarletteHTTPException,
        http_exception_handler,  # type: ignore[arg-type]
    )
    application.add_exception_handler(Exception, internal_error_handler)
    application.include_router(auth_router)
    application.include_router(auth_providers_router)
    application.include_router(admin_router)
    application.include_router(blacklist_router)
    application.include_router(callbacks_router)
    application.include_router(apps_router)
    application.include_router(approvals_router)
    application.include_router(messages_router)
    application.include_router(metrics_router)
    application.include_router(ops_router)
    application.include_router(replies_router)
    application.include_router(reports_router)
    application.include_router(sensitive_words_router)
    application.include_router(security_daily_router)
    application.include_router(signs_router)
    application.include_router(templates_router)
    application.include_router(users_router)
    application.include_router(vendor_test_router)
    application.include_router(web_messages_router)

    @application.get("/livez", tags=["system"])
    async def livez() -> JSONResponse:
        """只证明事件循环仍能处理请求，不访问任何外部依赖。"""

        return JSONResponse(
            content={"status": "alive"},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/healthz", tags=["system"], deprecated=True)
    async def healthz() -> JSONResponse:
        """兼容旧探针的存活别名；新部署必须使用 livez/readyz。"""

        return JSONResponse(
            content={"status": "ok"},
            headers={"Cache-Control": "no-store"},
        )

    @application.get(
        "/readyz",
        tags=["system"],
        responses={503: {"description": "必要依赖尚未满足接流条件"}},
    )
    async def readyz() -> JSONResponse:
        """检查必要依赖；响应不返回组件名、地址或失败原因。"""

        ready = await application.state.readiness_probe.ready()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
            headers={"Cache-Control": "no-store"},
        )

    return application


app = create_app()
