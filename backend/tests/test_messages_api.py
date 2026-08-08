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
from app.services.category import CategoryNotAllowed
from app.services.pipeline import (
    BatchResponse,
    IdempotencyConflict,
    SendRequest,
    VendorTestConsoleOnly,
)
from app.services.vendor_control_state import VendorControlStateUnavailable
from app.services.vendor_test_guard import VendorTestRecipientDenied
from app.services.vendor_test_recipient import (
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientForSend,
)


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
async def test_scheduling_dependency_reuses_api_database_and_redis_pools(
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

    class FakePolicyLoader:
        def __init__(self, selected_settings: object) -> None:
            assert selected_settings is settings

        async def load(self) -> object:
            return type("PolicyStub", (), {"approval_expire_hours": 24})()

    class FakeRepository:
        def __init__(self, selected_settings: object, *, pooled: bool) -> None:
            assert selected_settings is settings
            assert pooled is True
            repositories.append(self)

    monkeypatch.setattr(messages_module, "get_settings", lambda: settings)
    monkeypatch.setattr(messages_module, "SqlRuntimePolicyLoader", FakePolicyLoader)
    monkeypatch.setattr(messages_module, "SqlSchedulingRepository", FakeRepository)
    monkeypatch.setattr(
        messages_module,
        "redis_client",
        lambda redis_url: redis if redis_url == "redis://test" else None,
    )

    service = await messages_module.get_scheduling_service()

    assert service.repository is repositories[0]
    assert service.quota.redis is redis


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[ApiAppContext, SendRequest]] = []

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


async def allow_vendor_test_api_ready() -> None:
    return None


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
    }
    assert pipeline.calls[0][0].app_id == 7


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


def test_send_api_maps_live_test_recipient_denial_without_sensitive_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeniedPipeline:
        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            raise VendorTestRecipientDenied(1)

    async def fake_factory(app: ApiAppContext) -> DeniedPipeline:
        return DeniedPipeline()

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

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORBIDDEN",
        "message": "真实联调仅允许已登记测试号码",
        "detail": {"denied_count": 1},
    }
    assert "13800138000" not in response.text


def test_live_test_ordinary_api_send_is_console_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConsoleOnlyPipeline:
        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            raise VendorTestConsoleOnly

    async def fake_factory(_app: ApiAppContext) -> ConsoleOnlyPipeline:
        return ConsoleOnlyPipeline()

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

    assert response.status_code == 403
    assert response.json()["code"] == "VENDOR_TEST_CONSOLE_ONLY"


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


def test_controlled_api_uat_uses_api_key_and_protected_registered_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline()
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    async def fake_resolve(phone: str) -> VendorTestRecipientForSend:
        assert phone == "13800138000"
        return resolved

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
        raising=False,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        fake_resolve,
        raising=False,
    )

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["batch_no"] == "batch-1"
    app, request = pipeline.calls[0]
    assert app.app_id == 7
    assert request.category == "notice"
    assert request.channel == "api"
    assert request.mobiles == ()
    assert request.protected_mobiles[0].phone_enc == b"ciphertext-only"
    assert request.protected_hmac_candidates == resolved.hmac_candidates
    assert request.vendor_test_uat is True
    assert request.is_test is True
    assert request.biz_id == "api-uat-1"
    assert request.actor == ApplicationPrincipal(7, "app-iam", "平台部")
    assert "13800138000" not in response.text


def test_controlled_api_uat_supports_approved_template_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline()
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    async def fake_factory(app: ApiAppContext) -> FakePipeline:
        return pipeline

    async def fake_resolve(_phone: str) -> VendorTestRecipientForSend:
        return resolved

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        fake_resolve,
    )

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "template_id": 12,
            "template_params": ["张三", "123456"],
            "biz_id": "api-uat-template-1",
        },
    )

    assert response.status_code == 200
    _app, request = pipeline.calls[0]
    assert request.content is None
    assert request.template_id == 12
    assert request.template_params == ["张三", "123456"]
    assert request.vendor_test_uat is True
    assert request.is_test is True
    assert request.biz_id == "api-uat-template-1"


def test_controlled_api_uat_rejects_content_and_template_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = FakePipeline()
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    async def fake_factory(_app: ApiAppContext) -> FakePipeline:
        return pipeline

    async def fake_resolve(_phone: str) -> VendorTestRecipientForSend:
        return resolved

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        fake_resolve,
    )

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "直接内容",
            "template_id": 12,
            "template_params": ["张三"],
            "biz_id": "api-uat-both-1",
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert pipeline.calls == []


def test_controlled_api_uat_returns_completed_idempotent_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CompletedIdempotentPipeline(FakePipeline):
        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
            **_kwargs: object,
        ) -> BatchResponse:
            self.calls.append((app, request))
            return BatchResponse(
                "existing-batch",
                True,
                1,
                0,
                0,
                0,
                1,
                1,
                "completed",
                None,
                None,
            )

    pipeline = CompletedIdempotentPipeline()
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    async def fake_factory(_app: ApiAppContext) -> CompletedIdempotentPipeline:
        return pipeline

    async def fake_resolve(_phone: str) -> VendorTestRecipientForSend:
        return resolved

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        fake_resolve,
    )

    response = TestClient(make_app(), raise_server_exceptions=False).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-existing",
        },
    )

    assert response.status_code == 200
    assert response.json()["batch_no"] == "existing-batch"
    assert response.json()["idempotent"] is True
    assert response.json()["status"] == "completed"
    assert len(pipeline.calls) == 1


@pytest.mark.asyncio
async def test_controlled_api_uat_requires_live_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonLiveSettings:
        vendor_live_test = False

    monkeypatch.setattr(messages_module, "get_settings", lambda: NonLiveSettings())

    with pytest.raises(ApiError) as captured:
        await messages_module._require_vendor_test_api_ready()

    assert captured.value.status_code == 403
    assert captured.value.code == "VENDOR_TEST_MODE_REQUIRED"


@pytest.mark.asyncio
async def test_controlled_api_uat_fails_closed_and_sets_critical_pause_when_state_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveSettings:
        vendor_live_test = True

    class UnavailableState:
        def require_fresh(self) -> None:
            raise VendorControlStateUnavailable(
                "unavailable",
                requires_critical_pause=True,
            )

    monkeypatch.setattr(messages_module, "get_settings", lambda: LiveSettings())
    monkeypatch.setattr(
        messages_module,
        "VendorControlStateGuard",
        UnavailableState,
        raising=False,
    )
    pauses: list[str] = []

    async def pause(_settings: object) -> None:
        pauses.append("agent-stale")

    monkeypatch.setattr(
        messages_module,
        "_pause_vendor_test_agent_stale",
        pause,
        raising=False,
    )

    with pytest.raises(ApiError) as captured:
        await messages_module._require_vendor_test_api_ready()

    assert captured.value.status_code == 503
    assert captured.value.code == "CONTROL_AGENT_UNAVAILABLE"
    assert pauses == ["agent-stale"]


@pytest.mark.asyncio
async def test_controlled_api_uat_not_ready_does_not_set_critical_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveSettings:
        vendor_live_test = True

    class NotReadyState:
        def require_fresh(self) -> None:
            raise VendorControlStateUnavailable(
                "not ready",
                requires_critical_pause=False,
            )

    monkeypatch.setattr(messages_module, "get_settings", lambda: LiveSettings())
    monkeypatch.setattr(messages_module, "VendorControlStateGuard", NotReadyState)

    async def unexpected_pause(_settings: object) -> None:
        raise AssertionError("未激活或人工暂停不得升级为 critical pause")

    monkeypatch.setattr(
        messages_module,
        "_pause_vendor_test_agent_stale",
        unexpected_pause,
    )

    with pytest.raises(ApiError) as captured:
        await messages_module._require_vendor_test_api_ready()

    assert captured.value.status_code == 503
    assert captured.value.code == "CONTROL_AGENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_controlled_api_uat_pause_store_failure_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LiveSettings:
        vendor_live_test = True

    class CorruptState:
        def require_fresh(self) -> None:
            raise VendorControlStateUnavailable(
                "corrupt",
                requires_critical_pause=True,
            )

    monkeypatch.setattr(messages_module, "get_settings", lambda: LiveSettings())
    monkeypatch.setattr(messages_module, "VendorControlStateGuard", CorruptState)

    async def failed_pause(_settings: object) -> None:
        raise ConnectionError("sensitive backend detail")

    monkeypatch.setattr(messages_module, "_pause_vendor_test_agent_stale", failed_pause)

    with pytest.raises(ApiError) as captured:
        await messages_module._require_vendor_test_api_ready()

    assert captured.value.status_code == 503
    assert captured.value.code == "CONTROL_AGENT_PAUSE_UNAVAILABLE"
    assert "sensitive backend detail" not in caplog.text
    assert any(
        getattr(record, "error_type", None) == "ConnectionError" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_controlled_api_uat_resolver_delegates_to_protected_recipient_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    class Resolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def resolve_phone_for_send(self, phone: str) -> VendorTestRecipientForSend:
            self.calls.append(phone)
            return resolved

    resolver = Resolver()
    monkeypatch.setattr(
        messages_module,
        "_vendor_test_api_recipient_service",
        lambda: resolver,
        raising=False,
    )

    result = await messages_module._resolve_vendor_test_api_recipient("13800138000")

    assert result == resolved
    assert resolver.calls == ["13800138000"]


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (RecipientNotFound("internal"), 403, "FORBIDDEN"),
        (RecipientHmacIndexStale("internal"), 409, "STATE_CONFLICT"),
    ],
)
def test_controlled_api_uat_hides_recipient_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    pipeline = FakePipeline()

    async def fake_factory(_app: ApiAppContext) -> FakePipeline:
        return pipeline

    async def reject(_phone: str) -> VendorTestRecipientForSend:
        raise error

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
    )
    monkeypatch.setattr(messages_module, "_resolve_vendor_test_api_recipient", reject)

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-lookup",
        },
    )

    assert response.status_code == status_code
    assert response.json()["code"] == code
    assert "13800138000" not in response.text


def test_controlled_api_uat_authorizes_and_limits_before_recipient_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RejectingPipeline:
        async def preauthorize(self, app: ApiAppContext, category: str) -> None:
            assert app.app_id == 7 and category == "notice"
            events.append("preauthorize")
            raise CategoryNotAllowed("应用无权发送该消息类别")

        async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
            raise AssertionError("类别拒绝后不得进入发送流水线")

    async def fake_factory(_app: ApiAppContext) -> RejectingPipeline:
        return RejectingPipeline()

    async def unexpected_resolve(_phone: str) -> VendorTestRecipientForSend:
        events.append("recipient_lookup")
        raise AssertionError("类别拒绝后不得探测测试号码")

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)

    async def observe_control_state() -> None:
        events.append("control_state")

    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        observe_control_state,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        unexpected_resolve,
    )

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json={
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-denied",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CATEGORY_NOT_ALLOWED"
    assert events == ["preauthorize"]


@pytest.mark.parametrize(
    "body",
    [
        {
            "category": "verify",
            "mobiles": ["13800138000"],
            "content": "验证码123456",
            "biz_id": "api-uat-category",
        },
        {
            "category": "notice",
            "mobiles": ["13800138000", "13900139000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-count",
        },
        {
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
        },
        {
            "category": "notice",
            "mobiles": ["13800138000"],
            "content": "API 受控联调测试，请忽略。",
            "biz_id": "api-uat-extra",
            "scheduled_at": "2026-07-22T08:00:00+08:00",
        },
    ],
)
def test_controlled_api_uat_rejects_broader_send_shapes(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
) -> None:
    pipeline = FakePipeline()
    resolved = VendorTestRecipientForSend(
        id=9,
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
        hmac_candidates=((1, "b" * 64), (2, "a" * 64)),
    )

    async def fake_factory(_app: ApiAppContext) -> FakePipeline:
        return pipeline

    async def fake_resolve(_phone: str) -> VendorTestRecipientForSend:
        return resolved

    monkeypatch.setattr(messages_module, "_pipeline", fake_factory)
    monkeypatch.setattr(
        messages_module,
        "_require_vendor_test_api_ready",
        allow_vendor_test_api_ready,
    )
    monkeypatch.setattr(
        messages_module,
        "_resolve_vendor_test_api_recipient",
        fake_resolve,
    )

    response = TestClient(make_app()).post(
        "/api/v1/messages/uat-send",
        headers={"X-Api-Key": "valid"},
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAM"
    assert pipeline.calls == []


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
    app.dependency_overrides[messages_module.get_scheduling_service] = lambda: service

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
    }
    assert pipeline.calls[0][1].resend_of == "original-1"
