"""真实 PostgreSQL 审计落库的高风险操作契约测试。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.jwt import JwtClaims
from app.core.correlation import correlation_scope
from app.services.export_step_up import ExportStepUpService

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class FakeAuth:
    async def reauthenticate_current(
        self,
        claims: JwtClaims,
        password: str,
        ip: str,
    ) -> None:
        assert claims.account_id > 0
        assert password == "correct-password"
        assert ip == "127.0.0.1"


class FakeStepUpStore:
    def __init__(self) -> None:
        self.sets: list[tuple[str, str, int]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.sets.append((key, value, ex))

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        del script, numkeys, args
        return 0


@pytest.mark.asyncio
async def test_export_step_up_persists_real_audit_row() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    public_id = uuid4()
    claims = JwtClaims(
        8,
        18,
        "local",
        "operator01",
        "测试操作员",
        "平台部",
        "approver",
        1,
        "jti-step-up",
        "session-step-up",
    )

    async def audit_sink(event: AuditEvent) -> None:
        async with engine.begin() as connection:
            await insert_audit(connection, event)

    service = ExportStepUpService(
        FakeAuth(),
        FakeStepUpStore(),
        audit_sink=audit_sink,
    )
    try:
        with correlation_scope(uuid4()):
            token = await service.issue(
                claims=claims,
                password="correct-password",
                ip="127.0.0.1",
                public_id=public_id,
            )
        assert token
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT action, object_type, object_id, actor_account_id, role
                        FROM audit_log
                        WHERE action='export_step_up' AND object_id=:object_id
                        """
                    ),
                    {"object_id": str(public_id)},
                )
            ).mappings().one()
        assert row["action"] == "export_step_up"
        assert row["object_type"] == "export_task"
        assert int(row["actor_account_id"]) == 8
        assert row["role"] == "approver"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE action='export_step_up' AND object_id=:object_id"
                ),
                {"object_id": str(public_id)},
            )
        await engine.dispose()
