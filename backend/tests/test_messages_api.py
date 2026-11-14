from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import app.api.messages as messages_module
from app.core.apikey import ApiAppContext, get_api_key_authenticator
from app.core.auth.accounts import ActorPrincipal, ApplicationPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.roles import Role
from app.core.errors import (
    ApiError,
    api_error_handler,
    validation_error_handler,
)
from app.services.app_ratelimit import ApplicationRateLimitExceeded
from app.services.batch_query import BatchAccessScope
from app.services.pipeline import (
    BatchResponse,
    IdempotencyConflict,
    SendRequest,
)
from app.services.send_admission import SendAdmissionRejected


class FakeKeyAuth:
    async def authenticate(self, key: str) -> ApiAppContext:
        return ApiAppContext(7, "app-iam", "平台部", frozenset({"verify", "notice"}))


class RoleFacade:
    def __init__(self, role: Role) -> None:
        self.role: Role = role

    async def verify(self, token: str) -> JwtClaims:
        assert token == "jwt"
        return JwtClaims(
            11,
            101,
            "local",
            "user01",
            "测试用户",
            "业务一部",
            self.role,
        )


@pytest.mark.asyncio
async def test_scheduling_dependencies_reuse_api_pools_and_cancel_skips_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = type(
        "SettingsStub",
        (),
        {
            "database_url": "postgresql+asyncpg://test",
            "redis_control_url": "redis://test",
        },
    )()
    redis = object()
    repositories: list[object] = []
    policy_loads = 0

    class FakePolicyLoader:
        def __init__(self, selected_settings: object) -> None:
            assert selected_settings is settings

        async def load(self) -> object:
            nonlocal policy_loads
            policy_loads += 1
            return type("PolicyStub", (), {"approval_expire_hours": 7})()

    class FakeRepository:
        def __init__(self, selected_settings: object) -> None:
            assert selected_settings is settings
            repositories.append(self)

    monkeypatch.setattr(messages_module, "get_settings", lambda: settings)
    monkeypatch.setattr(messages_module, "SqlRuntimePolicyLoader", FakePolicyLoader)
    monkeypatch.setattr(messages_module, "SqlSchedulingRepository", FakeRepository)
    monkeypatch.setattr(
        messages_module,
        "redis_client",
        lambda redis_url: redis if redis_url == "redis://test" else None,
    )

    cancel_service = await messages_module.get_scheduling_cancel_service(
        messages_module.BatchAccessScope(app_id=1)
    )

    assert policy_loads == 0
    assert cancel_service.repository is repositories[0]
    assert cancel_service.quota.redis is redis

    reschedule_service = await messages_module.get_scheduling_service(
        messages_module.BatchAccessScope(app_id=1)
    )

    assert policy_loads == 1
    assert reschedule_service.repository is repositories[1]
    assert reschedule_service.quota.redis is redis
    assert reschedule_service.approval_expire_hours == 7


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[ApiAppContext, SendRequest]] = []
        self.status = "queued"
        self.idempotent = False

    async def replay_if_present(
        self,
        _app: ApiAppContext,
        _request: SendRequest,
    ) -> BatchResponse | None:
        return None

    async def preauthorize(self, app: ApiAppContext, category: str) -> object:
        return object()

    async def accept(
        self,
        app: ApiAppContext,
        request: SendRequest,
        **_kwargs: object,
    ) -> BatchResponse:
        self.calls.append((app, request))
        return BatchResponse(
            "batch-1",
            self.idempotent,
            1,
            0,
            0,
            0,
            1,
            1,
            self.status,
            None,
            None,
        )


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_api_key_authenticator] = FakeKeyAuth
    app.include_router(messages_module.router)
    return app


def test_send_api_uses_app_context_and_returns_complete_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline()

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "biz-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "batch_no": "batch-1",
        "idempotent": False,
        "accepted": 1,
        "removed_duplicate": 0,
        "removed_blacklist": 0,
        "removed_freq_limit": 0,
        "est_segments": 1,
        "quota_cost": 1,
        "status": "queued",
        "deferred_reason": None,
        "scheduled_at": None,
        "idempotency_expires_at": None,
    }
    assert pipeline.calls[0][0].app_id == 7


def test_send_omitting_biz_id_returns_422() -> None:
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PARAM"
    assert "biz_id" in str(body.get("detail"))


def test_send_serializes_completed_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnknownPipeline:
        async def preauthorize(self, _app: ApiAppContext, _category: str) -> object:
            return object()

        async def accept(
            self,
            _app: ApiAppContext,
            _request: SendRequest,
            **_kwargs: object,
        ) -> BatchResponse:
            return BatchResponse(
                "batch-unknown",
                True,
                1,
                0,
                0,
                0,
                1,
                1,
                "completed_unknown",
                None,
                None,
            )

    async def fake_factory(_app: ApiAppContext) -> UnknownPipeline:
        return UnknownPipeline()

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "biz-unknown",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed_unknown"
    assert response.json()["idempotent"] is True


def test_send_maps_idempotency_conflict_to_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictPipeline:
        async def preauthorize(self, _app: ApiAppContext, _category: str) -> object:
            return object()

        async def accept(
            self,
            _app: ApiAppContext,
            _request: SendRequest,
            **_kwargs: object,
        ) -> BatchResponse:
            raise IdempotencyConflict("同一幂等键已用于不同请求")

    async def fake_factory(_app: ApiAppContext) -> ConflictPipeline:
        return ConflictPipeline()

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "biz-conflict",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_send_api_rejects_unknown_fields_with_422() -> None:
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "scheduled_at_typo": "2026-08-08T00:00:00+08:00",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"


def test_send_api_rejects_invalid_phone_with_uniform_error() -> None:
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={"category": "verify", "mobiles": ["not-phone"], "content": "测试"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"


def test_send_api_returns_rate_limited_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedPipeline:
        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            raise ApplicationRateLimitExceeded("应用请求频率超限")

    async def fake_factory(app: ApiAppContext) -> RateLimitedPipeline:
        return RateLimitedPipeline()

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "测试",
            "biz_id": "biz-1",
        },
    )
    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMITED"
    assert response.headers["retry-after"] == "60"


def test_send_api_exposes_idempotency_expires_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    expires = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)

    class ExpiringPipeline:
        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            return BatchResponse(
                "batch-1",
                False,
                1,
                0,
                0,
                0,
                1,
                1,
                "queued",
                None,
                None,
                expires,
            )

    async def fake_factory(app: ApiAppContext) -> ExpiringPipeline:
        return ExpiringPipeline()

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "测试",
            "biz_id": "biz-1",
        },
    )
    assert response.status_code == 200
    assert response.headers["idempotency-expires-at"] == "2026-09-05T16:00:00+08:00"
    assert response.json()["idempotency_expires_at"] == "2026-09-05T16:00:00+08:00"


def test_send_api_maps_admission_closed_to_503_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedPipeline:
        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            raise SendAdmissionRejected("closed", "outbox_backlog", 60)

    async def fake_factory(app: ApiAppContext) -> ClosedPipeline:
        return ClosedPipeline()

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    response = TestClient(make_app()).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "测试",
            "biz_id": "biz-1",
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["detail"]["reason"] == "outbox_backlog"
    assert "broker" not in response.json()["message"].casefold()
    assert "worker" not in response.json()["message"].casefold()
    assert response.headers["retry-after"] == "60"


def test_send_api_blocks_source_ip_outside_app_allowlist_without_consuming_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RestrictedKeyAuth:
        async def authenticate(self, key: str) -> ApiAppContext:
            return ApiAppContext(
                7,
                "app-iam",
                "平台部",
                frozenset({"verify", "notice"}),
                allowed_ips=("203.0.113.0/24",),
            )

    class CountingPipeline:
        def __init__(self) -> None:
            self.calls = 0

        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
            **_kwargs: object,
        ) -> BatchResponse:
            self.calls += 1
            return BatchResponse(
                "batch-1",
                False,
                1,
                0,
                0,
                0,
                1,
                1,
                "queued",
                None,
                None,
            )

    pipeline = CountingPipeline()

    async def fake_factory(app: ApiAppContext) -> CountingPipeline:
        return pipeline

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_api_key_authenticator] = RestrictedKeyAuth
    app.include_router(messages_module.router)
    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)

    blocked = TestClient(app, client=("198.51.100.7", 12345)).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "biz-1",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "IP_NOT_ALLOWED"
    assert pipeline.calls == 0

    allowed = TestClient(app, client=("203.0.113.7", 12345)).post(
        "/api/v1/messages/send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "biz-1",
        },
    )
    assert allowed.status_code == 200
    assert pipeline.calls == 1


def test_scheduled_batch_cancel_and_reschedule_endpoints() -> None:
    # audited actions: batch_cancel, batch_reschedule
    class FakeScheduling:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def cancel(self, batch_no: str, scope: object) -> None:
            self.calls.append(("cancel", batch_no, scope))

        async def reschedule(self, batch_no: str, scope: object, scheduled_at: object) -> None:
            self.calls.append(("reschedule", batch_no, scope, scheduled_at))

    service = FakeScheduling()
    app = make_app()
    app.dependency_overrides[messages_module.get_scheduling_cancel_service] = lambda: service
    app.dependency_overrides[messages_module.get_scheduling_service] = lambda: service
    client = TestClient(app)
    headers = {"X-Api-Key": "valid"}
    cancelled = client.post(
        "/api/v1/messages/batches/batch-1/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert (
        client.post(
            "/api/v1/messages/batches/batch-1/reschedule",
            headers=headers,
            json={"scheduled_at": "2026-07-12T08:00:00+08:00"},
        ).status_code
        == 200
    )
    assert [call[0] for call in service.calls] == ["cancel", "reschedule"]


@pytest.mark.parametrize(
    ("role", "expected_status", "expected_scope"),
    [
        ("viewer", 403, None),
        ("approver", 403, None),
        ("operator", 200, BatchAccessScope(dept="业务一部")),
        ("admin", 200, BatchAccessScope(all_departments=True)),
    ],
)
def test_bearer_batch_writes_enforce_role_matrix(
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    expected_status: int,
    expected_scope: BatchAccessScope | None,
) -> None:
    class FakeScheduling:
        def __init__(self) -> None:
            self.scopes: list[BatchAccessScope] = []

        async def cancel(self, batch_no: str, scope: BatchAccessScope) -> None:
            assert batch_no == "batch-1"
            self.scopes.append(scope)

    service = FakeScheduling()
    monkeypatch.setattr(messages_module, "get_auth_facade", lambda: RoleFacade(role))
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_api_key_authenticator] = FakeKeyAuth
    app.include_router(messages_module.router)
    app.dependency_overrides[messages_module.get_scheduling_cancel_service] = lambda: service

    response = TestClient(app).post(
        "/api/v1/messages/batches/batch-1/cancel",
        headers={"Authorization": "Bearer jwt"},
    )

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["code"] == "FORBIDDEN"
        assert service.scopes == []
    else:
        assert service.scopes == [expected_scope]


def test_resend_failed_reenters_pipeline_and_returns_traceability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # audited action: batch_resend_failed
    pipeline = FakePipeline()

    class FakeResendService:
        async def build_request(
            self,
            batch_no: str,
            scope: BatchAccessScope,
            actor: ActorPrincipal | None = None,
        ) -> SendRequest:
            assert batch_no == "original-1"
            assert scope.app_id == 7
            assert actor == ApplicationPrincipal(7, "app-iam", "平台部")
            return SendRequest(
                "verify",
                ("13800138000",),
                content="验证码123456",
                resend_of=batch_no,
            )

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    app = make_app()
    app.dependency_overrides[messages_module.get_resend_service] = FakeResendService
    response = TestClient(app).post(
        "/api/v1/messages/batches/original-1/resend-failed",
        headers={"X-Api-Key": "valid"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "batch_no": "batch-1",
        "resend_of": "original-1",
        "accepted": 1,
        "status": "queued",
        "idempotent": False,
    }
    assert pipeline.calls[0][1].resend_of == "original-1"


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "sending",
        "scheduled",
        "pending_approval",
        "completed",
        "completed_unknown",
        "cancelled",
        "rejected",
        "expired",
        "balance_blocked",
    ],
)
def test_resend_failed_returns_the_existing_child_status(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    pipeline = FakePipeline()
    pipeline.status = status
    pipeline.idempotent = True

    class FakeResendService:
        async def build_request(
            self,
            batch_no: str,
            scope: BatchAccessScope,
            actor: ActorPrincipal | None = None,
        ) -> SendRequest:
            return SendRequest("notice", ("13800138000",), content="通知", resend_of=batch_no)

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    app = make_app()
    app.dependency_overrides[messages_module.get_resend_service] = FakeResendService
    response = TestClient(app).post(
        "/api/v1/messages/batches/original-1/resend-failed",
        headers={"X-Api-Key": "valid"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["batch_no"] == "batch-1"
    assert body["status"] == status
    assert body["idempotent"] is True
    assert "ResendResponseModel" not in response.text
    assert "literal_error" not in response.text


def test_resend_failed_hides_unexpected_status_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline()
    pipeline.status = "not-a-batch-status"
    pipeline.idempotent = True

    class FakeResendService:
        async def build_request(
            self,
            batch_no: str,
            scope: BatchAccessScope,
            actor: ActorPrincipal | None = None,
        ) -> SendRequest:
            return SendRequest("notice", ("13800138000",), content="通知", resend_of=batch_no)

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    app = make_app()
    app.dependency_overrides[messages_module.get_resend_service] = FakeResendService
    response = TestClient(app).post(
        "/api/v1/messages/batches/original-1/resend-failed",
        headers={"X-Api-Key": "valid"},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "ValidationError" not in response.text
    assert "ResendResponseModel" not in response.text


def test_resend_model_validation_is_an_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Literal

    from pydantic import TypeAdapter

    pipeline = FakePipeline()

    async def reject_internal(*_args: object, **_kwargs: object) -> BatchResponse:
        TypeAdapter(Literal["queued"]).validate_python("completed")
        raise AssertionError("invalid resend status must fail validation")

    pipeline.accept = reject_internal  # type: ignore[method-assign]

    class FakeResendService:
        async def build_request(
            self,
            batch_no: str,
            scope: BatchAccessScope,
            actor: ActorPrincipal | None = None,
        ) -> SendRequest:
            return SendRequest("notice", ("13800138000",), content="通知", resend_of=batch_no)

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    app = make_app()
    app.dependency_overrides[messages_module.get_resend_service] = FakeResendService
    response = TestClient(app).post(
        "/api/v1/messages/batches/original-1/resend-failed",
        headers={"X-Api-Key": "valid"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_ERROR",
        "message": "服务内部错误",
        "detail": None,
    }
    assert "literal_error" not in response.text
    assert "pydantic" not in response.text.lower()


