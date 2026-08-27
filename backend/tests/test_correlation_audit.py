from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.audit import AuditEvent, insert_audit, validate_audit_payload
from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import (
    CorrelationIdMiddleware,
    correlation_scope,
    current_correlation_id,
    parse_correlation_id,
)
from app.core.errors import internal_error_handler
from app.services.outbox import OutboxClaim, OutboxExecutor
from app.tasks import (
    bind_task_correlation,
    log_task_failure,
    reset_task_correlation,
)

CORRELATION_ID = UUID("30000000-0000-4000-8000-000000000009")
ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")


def test_http_request_id_is_canonical_and_locates_one_structured_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(Exception, internal_error_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal detail")

    with caplog.at_level(logging.ERROR, logger="app.core.errors"):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/boom",
            headers={"X-Request-ID": str(CORRELATION_ID)},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == str(CORRELATION_ID)
    assert response.json() == {
        "code": "INTERNAL_ERROR",
        "message": "服务内部错误",
        "detail": {"request_id": str(CORRELATION_ID)},
    }
    matching = [
        record
        for record in caplog.records
        if getattr(record, "correlation_id", None) == str(CORRELATION_ID)
    ]
    assert len(matching) == 1
    assert matching[0].exc_info is not None
    assert "sensitive internal detail" not in response.text


def test_invalid_external_request_id_is_replaced() -> None:
    assert parse_correlation_id("not-a-uuid") is None
    assert parse_correlation_id("not-a-uuid\r\ninject") is None
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(app).get("/", headers={"X-Request-ID": "not-a-uuid"})
    assert UUID(response.headers["X-Request-ID"]) != CORRELATION_ID


def test_task_failure_log_uses_message_header_without_logging_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task_id = "10000000-0000-4000-8000-000000000009"
    task = SimpleNamespace(
        name="app.tasks.example",
        request=SimpleNamespace(headers={"correlation_id": str(CORRELATION_ID)}),
    )
    bind_task_correlation(task_id=task_id, task=task)
    error = RuntimeError("sensitive task argument")
    try:
        with caplog.at_level(logging.ERROR, logger="app.tasks"):
            log_task_failure(
                task_id=task_id,
                exception=error,
                traceback=error.__traceback__,
                sender=task,
            )
    finally:
        reset_task_correlation(task_id=task_id)

    record = caplog.records[-1]
    assert record.correlation_id == str(CORRELATION_ID)  # type: ignore[attr-defined]
    assert record.task_id == task_id  # type: ignore[attr-defined]
    assert "sensitive task argument" not in record.getMessage()


@pytest.mark.parametrize(
    "payload",
    [
        {"phone": "masked"},
        {"phone_enc": ["ciphertext"]},
        {"access_token": "value"},
        {"callback_secret": "value"},
        {"request_body": {"safe": True}},
        {"nested": ["prefix 13800138000 suffix"]},
    ],
)
def test_application_audit_guard_rejects_pii_secret_and_request_body(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="audit payload"):
        validate_audit_payload(payload)


def test_application_audit_guard_allows_credential_change_required_flag() -> None:
    validate_audit_payload(
        {"provider_code": "local", "credential_change_required": True}
    )


class RecordingConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: object, params: dict[str, Any]) -> None:
        self.calls.append((str(statement), params))
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_transactional_audit_uses_caller_connection_and_propagates_failure() -> None:
    connection = RecordingConnection()
    with correlation_scope(CORRELATION_ID):
        await insert_audit(
            connection,
            AuditEvent(
                principal=ADMIN,
                role="admin",
                action="config_update",
                object_type="sys_config",
                object_id="vendor_qps",
                after={"configured": True},
            ),
        )

    sql, params = connection.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert params["correlation_id"] == CORRELATION_ID
    assert params["account_id"] == 1 and params["identity_id"] == 10

    failing = RecordingConnection(RuntimeError("database rejected audit"))
    with pytest.raises(RuntimeError, match="database rejected audit"):
        await insert_audit(
            failing,
            AuditEvent(principal=ADMIN, action="config_update"),
        )


@pytest.mark.asyncio
async def test_numeric_audit_object_id_binds_as_bigint_before_text() -> None:
    connection = RecordingConnection()
    with correlation_scope(CORRELATION_ID):
        await insert_audit(
            connection,
            AuditEvent(
                principal=ADMIN,
                role="admin",
                action="callback_retry",
                object_type="callback_task",
                object_id="12345",
                after={"status": "pending"},
            ),
        )

    sql, params = connection.calls[0]
    assert "CAST(CAST(:object_id AS bigint) AS text)" in sql
    assert isinstance(params["object_id"], int)
    assert params["object_id"] == 12345


@pytest.mark.asyncio
async def test_outbox_claim_restores_durable_correlation_for_effect() -> None:
    claim = OutboxClaim(
        UUID("10000000-0000-4000-8000-000000000009"),
        UUID("20000000-0000-4000-8000-000000000009"),
        "callback.ready",
        (9,),
        CORRELATION_ID,
    )

    class Repository:
        async def claim_execution(self, *_: object, **__: object) -> OutboxClaim:
            return claim

        async def heartbeat(self, *_: object, **__: object) -> bool:
            return True

        async def complete(self, *_: object, **__: object) -> None:
            return None

        async def fail_execution(self, *_: object, **__: object) -> None:
            return None

    async def effect(_: OutboxClaim) -> int:
        assert current_correlation_id() == CORRELATION_ID
        return 1

    assert (
        await OutboxExecutor(Repository()).run(
            claim.event_id,
            expected_type="callback.ready",
            effect=effect,
        )
        == 1
    )
