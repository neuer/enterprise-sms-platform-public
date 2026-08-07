"""Prometheus 无 PII 平台聚合指标端点。"""

from __future__ import annotations

import hmac
from functools import lru_cache
from ipaddress import ip_address
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST

from app.api.auth import ERROR_RESPONSE
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.metrics import MetricsService, render_prometheus
from app.services.metrics_repository import SqlMetricsRepository
from app.settings import Settings, get_settings

router = APIRouter(tags=["system"])
metrics_bearer = HTTPBearer(auto_error=False, scheme_name="MetricsBearerAuth")


@lru_cache
def get_metrics_service() -> MetricsService:
    """复用独立 metrics 数据库连接池，并对并发 scrape 做单飞合并。"""

    settings = get_settings()
    return MetricsService(
        SqlMetricsRepository(settings),
        collection_timeout_s=settings.metrics_collection_timeout_seconds,
        snapshot_ttl_s=settings.metrics_snapshot_ttl_seconds,
    )


def authorize_metrics(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(metrics_bearer),
    ],
    settings: Annotated[object, Depends(get_settings)],
) -> None:
    """同时校验固定抓取源网段与独立 Bearer secret，任一失败即拒绝。"""

    typed_settings = cast("Settings", settings)
    client_host = trusted_client_ip(request)
    try:
        client_ip = ip_address(client_host)
    except ValueError:
        raise ApiError(403, "FORBIDDEN", "无权访问该资源") from None
    if not any(client_ip in network for network in typed_settings.metrics_allowed_networks):
        raise ApiError(403, "FORBIDDEN", "无权访问该资源")
    expected_token = typed_settings.credential("metrics_scrape_token")
    if len(expected_token) < 32:
        raise RuntimeError("metrics scrape token does not meet minimum length")
    if (
        credentials is None
        or credentials.scheme.casefold() != "bearer"
        or not hmac.compare_digest(
            credentials.credentials,
            expected_token,
        )
    ):
        raise ApiError(401, "UNAUTHORIZED", "认证失败")


@router.get(
    "/metrics",
    summary="Prometheus 平台聚合指标",
    description=(
        "仅允许受控网段使用独立 Bearer secret 抓取；返回固定低基数平台指标，"
        "并记录快照年龄、事件循环延迟、连接池占用与进程 RSS；"
        "不含手机号、正文、密钥或错误原文。"
    ),
    response_class=Response,
    responses={
        200: {
            "description": "Prometheus 标准文本指标",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        401: {"description": "抓取凭据无效", **ERROR_RESPONSE},
        403: {"description": "抓取源不在允许网段", **ERROR_RESPONSE},
        500: {"description": "依赖不可用", **ERROR_RESPONSE},
    },
)
async def metrics(
    _: Annotated[None, Depends(authorize_metrics)],
    service: Annotated[MetricsService, Depends(get_metrics_service)],
) -> Response:
    """抓取实时共享事实；异常不得降级为陈旧或局部指标。"""

    body = render_prometheus(await service.collect())
    return Response(content=body, headers={"Content-Type": CONTENT_TYPE_LATEST})
