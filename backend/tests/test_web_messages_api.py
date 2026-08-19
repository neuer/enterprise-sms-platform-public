from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import app.api.web_messages as api
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler, validation_error_handler
from app.services.import_repository import ImportReservation, StoredImport
from app.services.imports import ImportLimits, ImportPhone, ImportResult
from app.services.pipeline import BatchResponse, SendRequest, VendorTestConsoleOnly
from app.services.vendor_test_guard import VendorTestRecipientDenied


class FakeFacade:
    def __init__(self, role: str = "operator") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(
            11,
            101,
            "local",
            "operator01",
            "操作员",
            "业务一部",
            self.role,  # type: ignore[arg-type]
        )


class FakeParser:
    limits = ImportLimits(expire_hours=6)

    def preflight(self, filename: str, source: object, *, size: int) -> None:
        assert filename == "phones.csv"
        assert size > 0
        assert source is not None

    async def parse(self, filename: str, source: object, *, size: int) -> ImportResult:
        assert filename == "phones.csv"
        assert size > 0
        return ImportResult(
            [ImportPhone(b"cipher", "a" * 64, "138****8000", 1, 2)],
            [],
        )


def test_approver_read_scope_is_global_or_explicit_department() -> None:
    claims = JwtClaims(11, 101, "local", "approver01", "审批员", "业务一部", "approver")

    assert api._query_scope(claims) == api.BatchAccessScope(all_departments=True)
    assert api._query_scope(claims, "市场部") == api.BatchAccessScope(dept="市场部")


class FakeImportRepository:
    def __init__(self) -> None:
        self.reserved: tuple[str, SecurityPrincipal] | None = None
        self.released: list[tuple[UUID, SecurityPrincipal]] = []
        self.source_file: str | None = None

    async def register(
        self,
        *,
        principal: SecurityPrincipal,
        filename: str,
        source_size: int,
        expire_hours: int,
        ip: str,
    ) -> StoredImport:
        assert principal.login_name == "operator01"
        assert filename == "phones.csv"
        assert source_size > 0
        assert expire_hours == 6
        assert ip == "0.0.0.0"
        return StoredImport(
            "11111111-1111-1111-1111-111111111111",
            0,
            0,
            0,
            0,
            None,
            datetime.now(UTC) + timedelta(hours=24),
            status="staging",
            source_file="import-11111111-1111-1111-1111-111111111111.smsx",
        )

    async def attach_source(self, import_id: UUID, source_file: str) -> None:
        assert import_id == UUID("11111111-1111-1111-1111-111111111111")
        self.source_file = source_file

    async def get_status(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> StoredImport | None:
        assert principal.account_id == 11
        return StoredImport(
            import_id,
            1,
            0,
            0,
            0,
            None,
            datetime.now(UTC) + timedelta(hours=24),
            status="ready",
        )

    async def fail_registration(self, import_id: UUID, error: str) -> None:
        raise AssertionError((import_id, error))

    async def persist(
        self,
        result: ImportResult,
        *,
        principal: SecurityPrincipal,
        filename: str,
        expire_hours: int,
    ) -> StoredImport:
        assert principal.login_name == "operator01"
        assert filename == "phones.csv"
        assert expire_hours == 6
        return StoredImport(
            "11111111-1111-1111-1111-111111111111",
            len(result.valid),
            result.invalid,
            result.duplicate,
            result.blacklisted,
            None,
            datetime.now(UTC) + timedelta(hours=24),
        )

    async def reserve(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> ImportReservation:
        self.reserved = (import_id, principal)
        return ImportReservation(
            UUID("22222222-2222-4222-8222-222222222222"),
            datetime.now(UTC) + timedelta(minutes=5),
            (ImportPhone(b"cipher", "a" * 64, "138****8000", 1, 2),),
        )

    async def release(
        self,
        reservation_id: UUID,
        *,
        principal: SecurityPrincipal,
    ) -> bool:
        self.released.append((reservation_id, principal))
        return False

    async def invalid_file(
        self,
        import_id: str,
        *,
        principal: SecurityPrincipal,
    ) -> Path | None:
        return None


class FakeCrypto:
    def decrypt_phone(
        self,
        payload: bytes,
        key_version: int,
        phone_hmac: str,
        *,
        table: str,
    ) -> str:
        assert payload == b"cipher" and key_version == 1
        assert phone_hmac == "a" * 64 and table == "import_phone"
        return "13800138000"


class FakePipeline:
    def __init__(self) -> None:
        self.request: object | None = None

    async def accept(self, app: object, request: object) -> BatchResponse:
        self.request = request
        return BatchResponse("batch-web", False, 1, 0, 0, 0, 1, 1, "queued", None, None)

    async def response_for(self, batch_no: str) -> BatchResponse:
        return BatchResponse(batch_no, True, 1, 0, 0, 0, 1, 1, "queued", None, None)


class FakeConfigStore:
    def __init__(self, *, quota_usage: int = 3412, quota_fails: bool = False) -> None:
        self.quota_usage = quota_usage
        self.quota_fails = quota_fails

    async def load_config(self, dept: str) -> dict[str, str]:
        assert dept == "业务一部"
        return {
            "unsubscribe_suffix": "回T退订",
            "approval_threshold": "100",
            "market_approval_threshold": "50",
        }

    async def read_dept_quota_usage(self, dept: str) -> int:
        assert dept == "业务一部"
        if self.quota_fails:
            raise RuntimeError("projection unavailable")
        return self.quota_usage


def make_client(
    *,
    repository: FakeImportRepository | None = None,
    role: str = "operator",
    store: FakeConfigStore | None = None,
) -> TestClient:
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    application.include_router(api.router)
    application.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    application.dependency_overrides[api.get_import_parser] = lambda: FakeParser()
    application.dependency_overrides[api.get_import_repository] = lambda: (
        repository or FakeImportRepository()
    )
    application.dependency_overrides[api.get_crypto_service] = lambda: FakeCrypto()
    application.dependency_overrides[api.get_pipeline_store] = lambda: (
        store or FakeConfigStore()
    )
    return TestClient(application)


def test_import_returns_pending_reference_without_phone_data(
    monkeypatch: MonkeyPatch,
) -> None:
    staged: list[str] = []
    bounded_calls: list[tuple[str, str]] = []

    async def run_inline(
        function: Callable[..., object],
        *args: object,
        timeout_s: float,
        pool: str = "default",
        **kwargs: object,
    ) -> object:
        assert timeout_s > 0
        bounded_calls.append((getattr(function, "__name__", ""), pool))
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    class FakeCodec:
        def __init__(self, *_: object) -> None:
            pass

        def stage(self, import_id: UUID, source: object, **_: object) -> str:
            assert import_id == UUID("11111111-1111-1111-1111-111111111111")
            assert source is not None
            staged.append("import-11111111-1111-1111-1111-111111111111.smsx")
            return "import-11111111-1111-1111-1111-111111111111.smsx"

    monkeypatch.setattr(api, "ImportFileCodec", FakeCodec)
    monkeypatch.setattr(api.CryptoService, "from_settings", lambda _settings: object())
    monkeypatch.setattr(api, "_enqueue_import", lambda import_id: None)
    monkeypatch.setattr(api, "run_bounded", run_inline)
    repository = FakeImportRepository()
    client = make_client(repository=repository)
    response = client.post(
        "/api/v1/web/messages/import",
        headers={"Authorization": "Bearer jwt"},
        files={"file": ("phones.csv", b"phone\n13800138000\n", "text/csv")},
    )
    assert response.status_code == 202
    assert response.json()["valid"] == 0
    assert response.json()["status"] == "pending"
    assert response.json()["invalid_download_url"] is None
    assert "13800138000" not in response.text
    assert "cipher" not in response.text
    assert staged == ["import-11111111-1111-1111-1111-111111111111.smsx"]
    assert ("preflight", "archive") in bounded_calls
    assert (
        repository.source_file
        == "import-11111111-1111-1111-1111-111111111111.smsx"
    )

    completed = client.get(
        "/api/v1/web/messages/import/11111111-1111-1111-1111-111111111111",
        headers={"Authorization": "Bearer jwt"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "ready"
    assert completed.json()["valid"] == 1


def test_preview_uses_server_billing_and_requires_market_consent() -> None:
    client = make_client()
    denied = client.post(
        "/api/v1/web/billing/preview",
        headers={"Authorization": "Bearer jwt"},
        json={"category": "market", "content": "活动", "accepted_count": 2},
    )
    allowed = client.post(
        "/api/v1/web/billing/preview",
        headers={"Authorization": "Bearer jwt"},
        json={
            "category": "market",
            "content": "活动",
            "accepted_count": 2,
            "consent_confirmed": True,
        },
    )
    assert denied.status_code == 422
    assert denied.json()["code"] == "CONSENT_REQUIRED"
    assert allowed.status_code == 200
    assert allowed.json()["quota_cost"] == 2
    assert allowed.json()["unsubscribe_appended"] is True
    assert allowed.json()["final_content"] == "活动回T退订"
    assert allowed.json()["quota"] == {"used": 3412, "limit": 0, "remaining": None}
    assert "deferred_reason" in allowed.json()


def test_preview_quota_summary_degrades_without_blocking() -> None:
    client = make_client(store=FakeConfigStore(quota_fails=True))
    response = client.post(
        "/api/v1/web/billing/preview",
        headers={"Authorization": "Bearer jwt"},
        json={"category": "notice", "content": "通知", "accepted_count": 1},
    )
    assert response.status_code == 200
    assert response.json()["quota"] is None
    assert response.json()["final_content"] == "通知"


def test_send_reserves_import_and_decrypts_only_for_pipeline(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = FakeImportRepository()
    pipeline = FakePipeline()

    @asynccontextmanager
    async def fake_pipeline(_: object) -> AsyncIterator[FakePipeline]:
        yield pipeline

    monkeypatch.setattr(api, "_pipeline_for", fake_pipeline)
    response = make_client(repository=repository).post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
            json={
                "category": "notice",
                "import_id": "11111111-1111-1111-1111-111111111111",
                "content": "维护通知",
                "remark": "变更窗口",
                "biz_id": "biz-1",
            },
    )
    assert response.status_code == 200
    assert repository.reserved == (
        "11111111-1111-1111-1111-111111111111",
        SecurityPrincipal(11, 101, "operator01", "业务一部", "operator"),
    )
    assert isinstance(pipeline.request, SendRequest)
    assert pipeline.request.mobiles == ["13800138000"]
    assert pipeline.request.biz_id == "biz-1"
    assert pipeline.request.import_reservation_id == UUID(
        "22222222-2222-4222-8222-222222222222"
    )
    assert pipeline.request.actor == SecurityPrincipal(
        11, 101, "operator01", "业务一部", "operator"
    )
    assert repository.released == []
    assert "13800138000" not in response.text


def test_web_send_rejects_unknown_fields_with_422() -> None:
    response = make_client().post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "维护通知",
            "consent_typo": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"


def test_import_validation_failure_releases_reservation(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = FakeImportRepository()

    class InvalidPipeline(FakePipeline):
        async def accept(self, app: object, request: object) -> BatchResponse:
            raise ValueError("校验失败")

    @asynccontextmanager
    async def fake_pipeline(_: object) -> AsyncIterator[InvalidPipeline]:
        yield InvalidPipeline()

    monkeypatch.setattr(api, "_pipeline_for", fake_pipeline)
    response = make_client(repository=repository).post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
            json={
                "category": "notice",
                "import_id": "11111111-1111-1111-1111-111111111111",
                "content": "维护通知",
                "biz_id": "biz-1",
            },
    )

    assert response.status_code == 400
    assert repository.released == [
        (
            UUID("22222222-2222-4222-8222-222222222222"),
            SecurityPrincipal(11, 101, "operator01", "业务一部", "operator"),
        )
    ]


def test_consumed_import_retry_returns_same_batch_without_decryption(
    monkeypatch: MonkeyPatch,
) -> None:
    class ConsumedRepository(FakeImportRepository):
        async def reserve(
            self,
            import_id: str,
            *,
            principal: SecurityPrincipal,
        ) -> ImportReservation:
            self.reserved = (import_id, principal)
            return ImportReservation(
                UUID("22222222-2222-4222-8222-222222222222"),
                datetime.now(UTC) + timedelta(minutes=5),
                consumed_batch_no="batch-original",
            )

    class ReplayPipeline(FakePipeline):
        async def accept(self, app: object, request: object) -> BatchResponse:
            raise AssertionError("consumed 导入包不得再次进入发送流水线")

    repository = ConsumedRepository()
    pipeline = ReplayPipeline()

    @asynccontextmanager
    async def fake_pipeline(_: object) -> AsyncIterator[ReplayPipeline]:
        yield pipeline

    monkeypatch.setattr(api, "_pipeline_for", fake_pipeline)
    response = make_client(repository=repository).post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
            json={
                "category": "notice",
                "import_id": "11111111-1111-1111-1111-111111111111",
                "content": "维护通知",
                "biz_id": "biz-1",
            },
    )

    assert response.status_code == 200
    assert response.json()["batch_no"] == "batch-original"
    assert response.json()["idempotent"] is True
    assert repository.released == []


def test_web_send_maps_live_test_recipient_denial_without_sensitive_detail(
    monkeypatch: MonkeyPatch,
) -> None:
    class DeniedPipeline:
        async def accept(self, app: object, request: object) -> BatchResponse:
            raise VendorTestRecipientDenied(1)

    @asynccontextmanager
    async def fake_pipeline(_: object) -> AsyncIterator[DeniedPipeline]:
        yield DeniedPipeline()

    monkeypatch.setattr(api, "_pipeline_for", fake_pipeline)
    response = make_client().post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
            json={
                "category": "notice",
                "mobiles": ["13800138000"],
                "content": "维护通知",
                "biz_id": "biz-1",
            },
    )

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORBIDDEN",
        "message": "真实联调仅允许已登记测试号码",
        "detail": {"denied_count": 1},
    }
    assert "13800138000" not in response.text


def test_live_test_ordinary_web_send_is_console_only(monkeypatch: MonkeyPatch) -> None:
    class ConsoleOnlyPipeline:
        async def accept(self, app: object, request: object) -> BatchResponse:
            raise VendorTestConsoleOnly

    @asynccontextmanager
    async def fake_pipeline(_: object) -> AsyncIterator[ConsoleOnlyPipeline]:
        yield ConsoleOnlyPipeline()

    monkeypatch.setattr(api, "_pipeline_for", fake_pipeline)
    response = make_client().post(
        "/api/v1/web/messages/send",
        headers={"Authorization": "Bearer jwt"},
            json={
                "category": "notice",
                "mobiles": ["13800138000"],
                "content": "维护通知",
                "biz_id": "biz-1",
            },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "VENDOR_TEST_CONSOLE_ONLY"


def test_viewer_cannot_write() -> None:
    response = make_client(role="viewer").post(
        "/api/v1/web/billing/preview",
        headers={"Authorization": "Bearer jwt"},
        json={"category": "notice", "content": "通知", "accepted_count": 1},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
