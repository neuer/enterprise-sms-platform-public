"""普通重发冲突只在当前来源权限和覆盖事实允许时复用旧结果。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.messages as messages_module
from app.core.apikey import ApiAppContext
from app.core.auth.accounts import ActorPrincipal
from app.services.batch_query import BatchAccessScope
from app.services.idempotency import IdempotencyConflict
from app.services.pipeline_contracts import BatchResponse, SendRequest
from app.services.resend import ExistingResend
from tests.test_messages_api import FakePipeline, make_app


@pytest.mark.parametrize("status,expected", [("completed", 200), (None, 409), ("invalid", 500)])
def test_repeated_action_can_return_existing_authorized_result(
    monkeypatch: pytest.MonkeyPatch, status: str | None, expected: int
) -> None:
    pipeline = FakePipeline()

    async def conflict(_app: ApiAppContext, _request: SendRequest) -> BatchResponse:
        raise IdempotencyConflict("不同操作者的旧指纹")

    pipeline.accept = conflict  # type: ignore[method-assign]

    class Service:
        async def build_request(
            self, batch_no: str, scope: BatchAccessScope, actor: ActorPrincipal | None = None
        ) -> SendRequest:
            return SendRequest("verify", ("13800138000",), resend_of=batch_no)

        async def existing_result(
            self, batch_no: str, scope: BatchAccessScope, channel: str
        ) -> ExistingResend | None:
            assert batch_no == "original-1" and scope.app_id == 7 and channel == "api"
            return ExistingResend("existing-child", 1, status) if status is not None else None

    async def factory(_app: ApiAppContext) -> FakePipeline:
        return pipeline

    monkeypatch.setattr(messages_module, "_pipeline", factory)
    app = make_app()
    app.dependency_overrides[messages_module.get_resend_service] = Service
    response = TestClient(app).post(
        "/api/v1/messages/batches/original-1/resend-failed", headers={"X-Api-Key": "valid"}
    )
    assert response.status_code == expected
    if expected == 200:
        assert response.json() == {
            "batch_no": "existing-child", "resend_of": "original-1", "accepted": 1,
            "status": "completed", "idempotent": True,
        }
