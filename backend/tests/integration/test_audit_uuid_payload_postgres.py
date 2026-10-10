"""审计载荷中的 UUID/随机引用不得因手机号形状片段被 ck_audit_payload_no_pii 拒绝。

str(uuid4()) 末段 12 位 hex 约 0.045% 会形成独立的「1 开头 11 位数字」。这里用固定的
碰撞值驱动真实仓储写入路径，确定性复现，不依赖随机命中；约束本身保持不变。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.audit import audit_uuid_ref
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import bind_connection_system_audit, close_runtime_resources
from app.services.security_daily import (
    SecurityDailyConfigurationUpdate,
    SecurityDailyControlResult,
)
from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.services.vendor_test_operation_repository import SqlVendorTestOperationRepository
from app.services.vendor_test_security_audit import SqlVendorTestSecurityAuditRepository
from tests.integration.test_ops_audit_postgres import _create_admin
from tests.test_security_daily import payload as report_payload

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

# 末段 hex 含独立的「1 开头 11 位数字」，与约束的手机号模式碰撞。
PHONE_SHAPED_A = UUID("00000000-0000-4000-8000-a12345678901")
PHONE_SHAPED_B = UUID("00000000-0000-4000-8000-b19876543210")
# vendor_test_manager 生成的 checkpoint 形如 vendor-activation-<UTC>-<12 位随机 hex>。
PHONE_SHAPED_CHECKPOINT = "vendor-activation-20261010T010203Z-a12345678901"
REPORT_DATE = date(2026, 6, 3)
SQL_REF = "translate(CAST(:uuid AS uuid)::text,'0123456789','ghijklmnop')"


@pytest.fixture
async def owner() -> AsyncIterator[tuple[AsyncEngine, URL]]:
    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(url)
    try:
        yield engine, url
    finally:
        await close_runtime_resources()
        await engine.dispose()


@pytest.fixture
async def principal(
    owner: tuple[AsyncEngine, URL],
) -> AsyncIterator[SecurityPrincipal]:
    engine, _ = owner
    created = await _create_admin(engine, login="audit-uuid-admin")
    try:
        yield created
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:id"),
                {"id": created.account_id},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_operation WHERE actor_account_id=:id"),
                {"id": created.account_id},
            )
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": created.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": created.account_id}
            )


def _uuid_sequence(*values: UUID) -> Any:
    remaining: Iterator[UUID] = iter(values)
    return lambda: next(remaining)


def _assert_no_raw_uuid(after_val: dict[str, object], *values: UUID) -> None:
    encoded = json.dumps(after_val)
    for value in values:
        assert str(value) not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_payload",
    [
        {"request_id": str(PHONE_SHAPED_A), "state": "failed"},
        {"operation_id": str(PHONE_SHAPED_A), "publish_state": "file_committed"},
        {"correlation_id": str(PHONE_SHAPED_A), "count": 1, "outcome": "succeeded"},
        {"count": 1, "operation_id": str(PHONE_SHAPED_A)},
        {"checkpoint_id": PHONE_SHAPED_CHECKPOINT, "count": 1},
    ],
)
async def test_phone_shaped_reference_is_rejected_inside_audit_payload(
    owner: tuple[AsyncEngine, URL],
    legacy_payload: dict[str, object],
) -> None:
    """对照：约束不放宽，旧载荷原样抄入碰撞引用必然被 CHECK 拒绝。"""

    engine, _ = owner
    async with engine.connect() as connection:
        await connection.begin()
        with correlation_scope():
            await bind_connection_system_audit(
                connection, actor_name="audit-uuid-probe", action="audit_uuid_probe"
            )
            with pytest.raises(IntegrityError) as caught:
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id,after_val
                        ) VALUES(
                          'audit-uuid-probe','system','system','audit_uuid_probe',
                          'audit_uuid_probe','probe',CAST(:after AS jsonb)
                        )
                        """
                    ),
                    {"after": json.dumps(legacy_payload)},
                )
        await connection.rollback()
    assert "ck_audit_payload_no_pii" in str(caught.value)


@pytest.mark.asyncio
async def test_security_daily_config_audit_keeps_phone_shaped_operation_correlatable(
    owner: tuple[AsyncEngine, URL],
    principal: SecurityPrincipal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置三阶段审计以无数字引用关联同一 operation，碰撞 UUID 也不得写入失败。"""

    engine, url = owner
    repository = SqlSecurityDailyRepository(cast(Any, SimpleNamespace(database_url=url)))
    monkeypatch.setattr(
        "app.services.security_daily_repository.uuid4", _uuid_sequence(PHONE_SHAPED_A)
    )
    async with engine.begin() as connection:
        saved_config = (
            await connection.execute(
                text("SELECT key,value FROM sys_config WHERE key LIKE 'security_daily_%'")
            )
        ).all()
        saved_recipients = (
            await connection.execute(
                text("SELECT position,address FROM security_daily_recipient")
            )
        ).all()
    try:
        with audit_principal_scope(principal), correlation_scope():
            configuration = await repository.update_configuration(
                SecurityDailyConfigurationUpdate(enabled=False, recipients=()),
                principal=principal,
                ip="10.8.0.8",
            )
        with correlation_scope():
            # 该方法吞掉 SQLAlchemyError；下方按行断言，确保审计确实落库而非被静默丢弃。
            await repository.mark_configuration_publish_state(
                config_version=configuration.config_version,
                publish_state="file_committed",
                operation_id=str(PHONE_SHAPED_A),
            )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        f"""
                        SELECT actor_subject_kind,object_id,after_val
                        FROM audit_log
                        WHERE action='security_daily_config_update'
                          AND after_val->>'operation_ref'={SQL_REF}
                        ORDER BY id
                        """
                    ),
                    {"uuid": str(PHONE_SHAPED_A)},
                )
            ).all()
            stored_operation = await connection.scalar(
                text(
                    "SELECT value FROM sys_config "
                    "WHERE key='security_daily_config_operation_id'"
                )
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM audit_log WHERE action='security_daily_config_update'")
            )
            for key, value in saved_config:
                await connection.execute(
                    text("UPDATE sys_config SET value=:value WHERE key=:key"),
                    {"key": key, "value": value},
                )
            await connection.execute(text("DELETE FROM security_daily_recipient"))
            for position, address in saved_recipients:
                await connection.execute(
                    text(
                        "INSERT INTO security_daily_recipient(position,address) "
                        "VALUES(:position,:address)"
                    ),
                    {"position": position, "address": address},
                )
    assert stored_operation == str(PHONE_SHAPED_A)
    assert [(kind, object_id, after["publish_state"]) for kind, object_id, after in rows] == [
        ("human", "default", "db_committed"),
        ("human", "default", "file_pending"),
        ("system", "default", "file_committed"),
    ]
    for _kind, _object_id, after in rows:
        assert after["operation_ref"] == audit_uuid_ref(PHONE_SHAPED_A)
        _assert_no_raw_uuid(after, PHONE_SHAPED_A)


@pytest.mark.asyncio
async def test_security_daily_delivery_audits_keep_phone_shaped_requests_correlatable(
    owner: tuple[AsyncEngine, URL],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """请求/失败/重试/unknown/结果五类投递审计都能写入，并能按引用连回请求行。"""

    engine, url = owner
    repository = SqlSecurityDailyRepository(cast(Any, SimpleNamespace(database_url=url)))
    monkeypatch.setattr(
        "app.services.security_daily_repository.uuid4",
        _uuid_sequence(PHONE_SHAPED_A, PHONE_SHAPED_B),
    )
    async with engine.begin() as connection:
        report_id = await connection.scalar(
            text(
                """
                INSERT INTO security_daily_report(
                  report_date,period_start,period_end,status,generation_source,
                  generation_status,payload
                ) VALUES(
                  :report_date,'2026-06-03T00:00:00+08:00','2026-06-03T23:59:59+08:00',
                  'normal','auto','ready',CAST(:payload AS jsonb)
                ) RETURNING id
                """
            ),
            {"report_date": REPORT_DATE, "payload": json.dumps(report_payload())},
        )
    try:
        report = await repository.get_report(int(report_id))
        assert report is not None
        with correlation_scope():
            first = await repository.request_delivery(report, "send", system=True)
        assert first.request_id == PHONE_SHAPED_A
        with correlation_scope():
            await repository.mark_request_failed(PHONE_SHAPED_A, "独立投递器不可用")
        with correlation_scope():
            second = await repository.request_delivery(report, "retry", system=True)
        assert second.request_id == PHONE_SHAPED_B
        with correlation_scope():
            await repository.mark_request_unknown(PHONE_SHAPED_B, "投递结果未知")
        with correlation_scope():
            await repository.apply_control_result(
                SecurityDailyControlResult(
                    request_id=PHONE_SHAPED_B,
                    report_date=REPORT_DATE,
                    state="sent",
                    completed_at=datetime.fromisoformat("2026-06-04T08:10:00+08:00"),
                )
            )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT audit.action,request.request_id,audit.after_val
                        FROM audit_log audit
                        LEFT JOIN security_daily_delivery_request request
                          ON audit.after_val->>'delivery_ref'=translate(
                               request.request_id::text,'0123456789','ghijklmnop'
                             )
                        WHERE audit.object_type='security_daily_report'
                          AND audit.object_id=:report_date
                          AND audit.action<>'security_daily_generated'
                        ORDER BY audit.id
                        """
                    ),
                    {"report_date": REPORT_DATE.isoformat()},
                )
            ).all()
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE object_type='security_daily_report' "
                    "AND object_id=:report_date"
                ),
                {"report_date": REPORT_DATE.isoformat()},
            )
            await connection.execute(
                text("DELETE FROM security_daily_delivery_request WHERE report_id=:id"),
                {"id": report_id},
            )
            await connection.execute(
                text("DELETE FROM security_daily_report WHERE id=:id"), {"id": report_id}
            )
    assert [
        (action, request_id, after.get("status") or after.get("state"))
        for action, request_id, after in rows
    ] == [
        ("security_daily_send", PHONE_SHAPED_A, "requested"),
        ("security_daily_delivery_result", PHONE_SHAPED_A, "failed"),
        ("security_daily_retry", PHONE_SHAPED_B, "requested"),
        ("security_daily_delivery_result", PHONE_SHAPED_B, "unknown"),
        ("security_daily_delivery_result", PHONE_SHAPED_B, "sent"),
    ]
    for _action, request_id, after in rows:
        assert after["delivery_ref"] == audit_uuid_ref(request_id)
        _assert_no_raw_uuid(after, PHONE_SHAPED_A, PHONE_SHAPED_B)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["vendor_test_step_up", "vendor_test_seal_session"])
async def test_vendor_security_audit_persists_phone_shaped_correlation_in_object_id(
    owner: tuple[AsyncEngine, URL],
    principal: SecurityPrincipal,
    action: str,
) -> None:
    """correlation UUID 只落 object_id，碰撞手机号模式也不得让安全事件写入失败。"""

    engine, url = owner
    audit = SqlVendorTestSecurityAuditRepository(cast(Any, SimpleNamespace(database_url=url)))
    with audit_principal_scope(principal), correlation_scope():
        await audit.record(
            correlation_id=str(PHONE_SHAPED_A),
            principal=principal,
            action=cast(Any, action),
            outcome="succeeded",
        )
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT object_type,object_id,after_val FROM audit_log
                    WHERE action=:action AND actor_account_id=:account_id
                    """
                ),
                {"action": action, "account_id": principal.account_id},
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("vendor_test_security", str(PHONE_SHAPED_A), {"count": 1, "outcome": "succeeded"})
    ]


@pytest.mark.asyncio
async def test_vendor_operation_audits_persist_phone_shaped_operation_and_checkpoint(
    owner: tuple[AsyncEngine, URL],
    principal: SecurityPrincipal,
) -> None:
    """operation UUID 只落 object_id，checkpoint 原值保留在 operation 行，终态不会卡在 running。"""

    engine, url = owner
    repository = SqlVendorTestOperationRepository(cast(Any, SimpleNamespace(database_url=url)))
    operation_id = str(PHONE_SHAPED_B)
    with audit_principal_scope(principal), correlation_scope():
        requested = await repository.reserve_start(
            operation_id,
            "activate",
            principal=principal,
            conflicting_types=frozenset({"activate"}),
        )
    assert requested.status == "requested"
    with correlation_scope():
        completed = await repository.complete(
            operation_id,
            status="succeeded",
            safe_code=None,
            checkpoint_id=PHONE_SHAPED_CHECKPOINT,
        )
    assert completed.status == "succeeded"
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT audit.action,audit.actor_subject_kind,audit.after_val,
                           operation.checkpoint_id
                    FROM audit_log audit
                    JOIN vendor_test_operation operation
                      ON operation.id::text=audit.object_id
                    WHERE audit.object_type='vendor_test_operation'
                      AND audit.object_id=:operation_id
                    ORDER BY audit.id
                    """
                ),
                {"operation_id": operation_id},
            )
        ).all()
        await connection.execute(
            text("DELETE FROM audit_log WHERE object_id=:operation_id"),
            {"operation_id": operation_id},
        )
        await connection.commit()
    assert [tuple(row) for row in rows] == [
        ("vendor_test_operation_requested", "human", {"count": 1}, PHONE_SHAPED_CHECKPOINT),
        (
            "vendor_test_operation_completed",
            "system",
            {"checkpoint_recorded": True, "count": 1},
            PHONE_SHAPED_CHECKPOINT,
        ),
    ]
