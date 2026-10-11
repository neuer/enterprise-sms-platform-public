"""真实签名和最小权限下，审计类型约束与应用校验成对验证。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.audit import AuditEvent, insert_audit, validate_audit_payload
from app.core.runtime_resources import bind_connection_audit_subject
from tests.integration.audit_fixtures import live_audit_principal as live_audit_principal
from tests.integration.test_ops_audit_postgres import accept_runtime as accept_runtime

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

INVALID = [
    {"batch_no": value}
    for value in (
        "13800138000",
        13800138000,
        [13800138000],
        {"count": 1},
        True,
        13800138000.0,
        None,
        "a" * 31,
        "A" * 32,
        "１" * 32,
    )
] + [
    {"nested": {"batch_no": "13800138000"}},
    {"count": 13800138000},
    {"count": 1.3800138e10},
    {"counts": [13800138000]},
    {"access_token": "opaque"},
    {"value_enc": "opaque"},
    {"secret": "opaque"},
    {"password": "opaque"},
    {"body": "opaque"},
    {"count": float("nan")},
    {"count": float("inf")},
    [],
    "value",
    1,
    True,
]


async def _bind(connection, principal):
    await bind_connection_audit_subject(
        connection,
        subject_kind="human",
        actor_name=principal.login_name,
        account_id=principal.account_id,
        identity_id=principal.identity_id,
    )


async def _direct(connection, principal, payload, field):
    assert field in {"before_val", "after_val"}
    await connection.execute(
        text(f"""
            INSERT INTO audit_log(actor,actor_subject_kind,actor_account_id,
              actor_identity_id,action,{field})
            VALUES(:actor,'human',:account,:identity,'audit_shape_probe',
              CAST(:payload AS jsonb))
        """),
        {
            "actor": principal.login_name,
            "account": principal.account_id,
            "identity": principal.identity_id,
            "payload": payload,
        },
    )


@pytest.mark.parametrize("payload", INVALID)
@pytest.mark.parametrize("field", ["before_val", "after_val"])
async def test_rejects_with_runtime_role_and_rolls_back(
    accept_runtime,
    live_audit_principal,
    payload,
    field,
):
    owner, url = accept_runtime
    principal = live_audit_principal
    engine = create_async_engine(url)
    key = "audit_probe_" + uuid4().hex
    try:
        for direct in (False, True):
            with pytest.raises(DBAPIError if direct else ValueError):
                async with engine.begin() as connection:
                    assert await connection.scalar(text("SELECT current_user")) == "sms_accept"
                    await _bind(connection, principal)
                    await connection.execute(
                        text("INSERT INTO sys_config(key,value) VALUES(:key,'1')"),
                        {"key": key},
                    )
                    if direct:
                        await _direct(connection, principal, json.dumps(payload), field)
                    else:
                        event = AuditEvent(
                            principal=principal,
                            action="audit_shape_probe",
                            **{"before" if field == "before_val" else "after": payload},
                        )
                        await insert_audit(connection, event)
            async with owner.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM sys_config WHERE key=:key"),
                        {"key": key},
                    )
                    == 0
                )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"batch_no": "a13800138000" + "f" * 20},
        {"count": 0, "maximum": 9223372036854775807, "enabled": True},
        {"count": 1e100, "ratio": 0.125, "labels": ["中文", None]},
    ],
)
async def test_legal_values_preserve_meaning(accept_runtime, live_audit_principal, payload):
    _, url = accept_runtime
    engine = create_async_engine(url)
    try:
        validate_audit_payload(payload)
        async with engine.begin() as connection:
            await _bind(connection, live_audit_principal)
            await insert_audit(
                connection,
                AuditEvent(
                    principal=live_audit_principal,
                    action="audit_shape_probe",
                    after=payload,
                ),
            )
            await _direct(
                connection,
                live_audit_principal,
                None if payload is None else json.dumps(payload),
                "before_val",
            )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await _bind(connection, live_audit_principal)
                await _direct(connection, live_audit_principal, "null", "after_val")
    finally:
        await engine.dispose()


async def test_upgrade_preserves_legacy_null_batch_evidence(
    accept_runtime,
    live_audit_principal,
):
    """仅测试建模旧 writer：历史缺省不改写，新写入仍由触发器拒绝。"""
    owner, url = accept_runtime
    principal = live_audit_principal
    engine = create_async_engine(url)
    try:
        async with owner.begin() as connection:
            await connection.execute(text("DROP TRIGGER trg_audit_payload_shape ON audit_log"))
        async with engine.begin() as connection:
            await _bind(connection, principal)
            await _direct(connection, principal, '{"batch_no":null}', "after_val")
        async with owner.begin() as connection:
            original = (
                await connection.execute(
                    text(
                        "SELECT id,created_at,after_val FROM audit_log WHERE actor_account_id=:id"
                    ),
                    {"id": principal.account_id},
                )
            ).one()

            def migrate(sync_connection):
                path = (
                    Path(__file__).resolve().parents[2]
                    / "migrations/versions/0121_audit_payload_shape.py"
                )
                spec = importlib.util.spec_from_file_location("audit_shape_migration", path)
                assert spec is not None and spec.loader is not None
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.upgrade()
                    migration.downgrade()  # 回退版本标记不得撤销安全保护。

            await connection.run_sync(migrate)
            current = (
                await connection.execute(
                    text(
                        "SELECT id,created_at,after_val FROM audit_log WHERE actor_account_id=:id"
                    ),
                    {"id": principal.account_id},
                )
            ).one()
            assert current == original
        with pytest.raises(DBAPIError, match="invalid audit batch reference"):
            async with engine.begin() as connection:
                await _bind(connection, principal)
                await _direct(connection, principal, '{"batch_no":null}', "after_val")
    finally:
        await engine.dispose()
