"""九条遗漏列表在 SQL 前拒绝越界页码，避免 OFFSET 变成 500。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import app.api.messages as messages_api
import app.api.ops as ops_api
import app.api.security_daily as security_daily_api
import app.api.sensitive_words as sensitive_api
import app.api.web_messages as web_messages_api
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler, validation_error_handler
from app.core.query_bounds import PAGE_MAX
from app.services.batch_query import BatchAccessScope, BatchQueryService
from app.services.operations_query import (
    OperationsQueryService,
    SqlOperationsQueryRepository,
)
from app.services.ops import AlertQuery, OpsPage, RawLogQuery, UnmatchedQuery
from app.services.ops_repository import SqlOpsRepository
from app.services.outbox import OutboxEventPage
from app.services.outbox_repository import SqlOutboxRepository
from app.services.security_daily.contract import SecurityDailyPage, SecurityDailyQuery
from app.services.security_daily.service import SecurityDailyService
from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.services.sensitive import SensitiveWordManager, SensitiveWordPage
from app.services.sensitive_repository import SqlSensitiveWordRepository

HUGE_PAGES = (
    PAGE_MAX + 1,
    0,
    -1,
    2**63,
    2**63 + 1,
    10**30,
)


class AdminFacade:
    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(1, 101, "local", "admin01", "管理员", "平台部", "admin", 1, "jti", "sid")


class Counting:
    def __init__(self) -> None:
        self.calls = 0

    def hit(self) -> None:
        self.calls += 1


def _app(*routers: Any, overrides: dict[Any, Any] | None = None) -> TestClient:
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    for router in routers:
        application.include_router(router)
    application.dependency_overrides[get_auth_facade] = lambda: AdminFacade()
    if overrides:
        application.dependency_overrides.update(overrides)
    return TestClient(application)


def _assert_invalid(response: Any) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAM"
    assert set(body) == {"code", "message", "detail"}
    assert "asyncpg" not in response.text
    assert "DataError" not in response.text


@pytest.mark.parametrize("page", HUGE_PAGES)
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/v1/web/admin/sensitive-words", None),
        ("get", "/api/v1/web/admin/security-daily/reports", None),
        ("get", "/api/v1/web/admin/alerts", None),
        ("get", "/api/v1/web/admin/outbox/events", None),
        ("get", "/api/v1/web/admin/raw-logs", None),
        ("get", "/api/v1/web/admin/chunks/uncertain", None),
        ("post", "/api/v1/web/admin/unmatched-reports", {}),
        ("post", "/api/v1/web/messages", {"phone": "13800138000"}),
        ("get", "/api/v1/messages/batches/B202610270001/details", None),
    ],
)
def test_authenticated_overflow_pages_are_rejected_before_queries(
    method: str,
    path: str,
    body: dict[str, object] | None,
    page: int,
) -> None:
    counter = Counting()

    class SensitiveManager:
        async def list_page(self, **_filters: object) -> SensitiveWordPage:
            counter.hit()
            return SensitiveWordPage(total=0, items=[])

    class DailyService:
        async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
            counter.hit()
            return SecurityDailyPage((), 0, query.page, query.page_size)

    class OpsRepository:
        async def list_alerts(self, query: AlertQuery) -> OpsPage[Any]:
            counter.hit()
            return OpsPage((), 0, query.page, query.page_size)

        async def list_raw_logs(self, query: RawLogQuery) -> OpsPage[Any]:
            counter.hit()
            return OpsPage((), 0, query.page, query.page_size)

        async def list_uncertain(self, page_no: int, page_size: int) -> OpsPage[Any]:
            counter.hit()
            return OpsPage((), 0, page_no, page_size)

    class OutboxRepository:
        async def list_events(
            self,
            _state: str | None,
            page_no: int,
            page_size: int,
        ) -> OutboxEventPage:
            counter.hit()
            return OutboxEventPage((), 0, page_no, page_size)

    class OpsService:
        async def list_unmatched(
            self,
            _phone: str | None,
            _start: object,
            _end: object,
            page_no: int,
            page_size: int,
        ) -> OpsPage[Any]:
            counter.hit()
            return OpsPage((), 0, page_no, page_size)

    class MessageService:
        async def search_messages(self, **_kwargs: object) -> Any:
            counter.hit()
            raise AssertionError("message search must not run")

    class Auditor:
        async def record(self, **_kwargs: object) -> None:
            counter.hit()

    class BatchQueries:
        async def list_details(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            counter.hit()
            return {"total": 0, "items": []}

    client = _app(
        sensitive_api.router,
        security_daily_api.router,
        ops_api.router,
        web_messages_api.router,
        messages_api.router,
        overrides={
            sensitive_api.get_sensitive_word_manager: lambda: SensitiveManager(),
            security_daily_api.get_security_daily_service: lambda: DailyService(),
            ops_api.get_ops_repository: lambda: OpsRepository(),
            ops_api.get_outbox_repository: lambda: OutboxRepository(),
            ops_api.get_ops_service: lambda: OpsService(),
            web_messages_api.get_operations_query_service: lambda: MessageService(),
            web_messages_api.get_sensitive_read_auditor: lambda: Auditor(),
            messages_api.get_batch_auth_facade: lambda: AdminFacade(),
        },
    )
    original_batch_queries = messages_api._batch_queries
    messages_api._batch_queries = lambda: BatchQueries()  # type: ignore[method-assign]
    params = {"page": str(page), "page_size": "1", "size": "1"}
    payload = {**(body or {}), "page": page}
    if path.endswith("/messages"):
        payload["size"] = 1
    else:
        payload["page_size"] = 1
    try:
        if method == "get":
            response = client.get(path, params=params, headers={"Authorization": "Bearer jwt"})
        else:
            response = client.post(
                path,
                json=payload,
                headers={"Authorization": "Bearer jwt"},
            )
    finally:
        messages_api._batch_queries = original_batch_queries
    _assert_invalid(response)
    assert counter.calls == 0


def test_unauthenticated_overflow_page_stays_unauthorized() -> None:
    counter = Counting()

    class OpsRepository:
        async def list_alerts(self, query: AlertQuery) -> OpsPage[Any]:
            counter.hit()
            return OpsPage((), 0, query.page, query.page_size)

    client = _app(
        ops_api.router,
        overrides={ops_api.get_ops_repository: lambda: OpsRepository()},
    )
    response = client.get("/api/v1/web/admin/alerts", params={"page": 2**63 + 1})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert counter.calls == 0


def test_page_max_still_reaches_the_query() -> None:
    seen: dict[str, int] = {}

    class OpsRepository:
        async def list_alerts(self, query: AlertQuery) -> OpsPage[Any]:
            seen["page"] = query.page
            return OpsPage((), 0, query.page, query.page_size)

    client = _app(
        ops_api.router,
        overrides={ops_api.get_ops_repository: lambda: OpsRepository()},
    )
    response = client.get(
        "/api/v1/web/admin/alerts",
        params={"page": PAGE_MAX, "page_size": 1},
        headers={"Authorization": "Bearer jwt"},
    )
    assert response.status_code == 200
    assert seen["page"] == PAGE_MAX
    assert response.json()["page"] == PAGE_MAX


@pytest.mark.asyncio
async def test_repositories_reject_overflow_before_opening_sql() -> None:
    sensitive = SqlSensitiveWordRepository.__new__(SqlSensitiveWordRepository)
    daily = SqlSecurityDailyRepository.__new__(SqlSecurityDailyRepository)
    ops = SqlOpsRepository.__new__(SqlOpsRepository)
    outbox = SqlOutboxRepository.__new__(SqlOutboxRepository)
    messages = SqlOperationsQueryRepository.__new__(SqlOperationsQueryRepository)
    batches = BatchQueryService.__new__(BatchQueryService)

    def refuse_sql(self: object) -> Any:
        raise AssertionError("sql engine opened")

    for target in (sensitive, daily, ops, outbox, messages, batches):
        target._engine = refuse_sql.__get__(target, type(target))  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="out of bounds"):
        await sensitive.list_page(keyword=None, page=PAGE_MAX + 1, size=1)
    with pytest.raises(ValueError, match="out of bounds"):
        await daily.list_reports(SecurityDailyQuery(None, None, None, None, None, PAGE_MAX + 1, 20))
    with pytest.raises(ValueError, match="out of bounds"):
        await ops.list_alerts(AlertQuery(None, None, None, None, 2**63, 1))
    with pytest.raises(ValueError, match="out of bounds"):
        await ops.list_raw_logs(RawLogQuery(None, None, 10**30, 20))
    with pytest.raises(ValueError, match="out of bounds"):
        await ops.list_uncertain(0, 20)
    with pytest.raises(ValueError, match="out of bounds"):
        await ops.list_unmatched(UnmatchedQuery((), None, None, -1, 20))
    with pytest.raises(ValueError, match="out of bounds"):
        await outbox.list_events(None, PAGE_MAX + 1, 20)
    with pytest.raises(ValueError, match="out of bounds"):
        await messages.search_messages(
            phone_hmacs=(),
            start=None,
            end=None,
            category=None,
            status=None,
            page=2**63 + 1,
            size=20,
            scope=BatchAccessScope(all_departments=True),
        )
    with pytest.raises(ValueError, match="out of bounds"):
        await batches.list_details(
            "B202610270001",
            BatchAccessScope(all_departments=True),
            status=None,
            page=10**30,
            size=100,
        )


@pytest.mark.asyncio
async def test_services_reject_overflow_before_repositories() -> None:
    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(name)

    manager = SensitiveWordManager(Exploding(), Exploding())
    with pytest.raises(ValueError, match="out of bounds"):
        await manager.list_page(page=PAGE_MAX + 1, size=20)

    queries = OperationsQueryService(Exploding(), Exploding())
    with pytest.raises(ValueError, match="out of bounds"):
        await queries.search_messages(
            phone="13800138000",
            start=None,
            end=None,
            category=None,
            status=None,
            page=2**63,
            size=20,
            scope=BatchAccessScope(all_departments=True),
        )

    daily = SecurityDailyService(Exploding(), Exploding())
    with pytest.raises(ValueError, match="out of bounds"):
        await daily.list_reports(SecurityDailyQuery(None, None, None, None, None, 0, 20))
