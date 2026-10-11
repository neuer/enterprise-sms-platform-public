"""安全日报在真实运行角色下的授权、发布意图及审计原子性。"""

import pytest
from sqlalchemy import text

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent
from app.services.security_daily import SecurityDailyConfigurationUpdate, SecurityDailyStateConflict
from app.services.security_daily.contract import security_daily_write_parameters
from app.services.security_daily_repository import SqlSecurityDailyRepository
from tests.integration.test_config_authorization_postgres import (  # noqa: F401
    config_context,
    flow_owner,
    pytestmark,
)


async def daily_snapshot(owner, principal):
    async with owner.connect() as c:
        configs = (
            (
                await c.execute(
                    text(
                        "SELECT key,value,updated_by,updated_at FROM sys_config "
                        "WHERE starts_with(key,'security_daily_') ORDER BY key"
                    )
                )
            )
            .mappings()
            .all()
        )
        recipients = (
            (
                await c.execute(
                    text("SELECT position,address FROM security_daily_recipient ORDER BY position")
                )
            )
            .mappings()
            .all()
        )
        audit_count = await c.scalar(
            text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
            {"id": principal.account_id},
        )
        return [dict(row) for row in configs], [dict(row) for row in recipients], audit_count


@pytest.fixture
async def daily_context(config_context):  # noqa: F811
    owner, principal, settings, _, _ = config_context
    before = await daily_snapshot(owner, principal)
    repo = SqlSecurityDailyRepository(settings)
    try:
        with audit_principal_scope(principal), correlation_scope():
            yield owner, principal, repo
    finally:
        async with owner.begin() as c:
            await c.execute(
                text(
                    "DELETE FROM sys_config WHERE starts_with(key,'security_daily_') "
                    "AND NOT (key=ANY(:keys))"
                ),
                {"keys": [row["key"] for row in before[0]]},
            )
            for row in before[0]:
                await c.execute(
                    text(
                        "UPDATE sys_config SET value=:value,updated_by=:updated_by,"
                        "updated_at=:updated_at "
                        "WHERE key=:key"
                    ),
                    row,
                )
            await c.execute(text("DELETE FROM security_daily_recipient"))
            for row in before[1]:
                await c.execute(
                    text(
                        "INSERT INTO security_daily_recipient(position,address) "
                        "VALUES(:position,:address)"
                    ),
                    row,
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "stale", "parameters", "revoked", "audit-failure"])
async def test_daily_write_matrix(daily_context, case):
    owner, principal, repo = daily_context
    current = await repo.configuration()
    version = current.config_version + (case == "stale")
    update = SecurityDailyConfigurationUpdate(False, ("synthetic@example.test",), "")
    grant = AdminAuthorization(
        principal.account_id,
        principal.identity_id,
        1,
        "local",
        admin_intent(
            "security_daily_config_update",
            "default",
            security_daily_write_parameters(update, version),
        ),
    )
    if case == "parameters":
        update = SecurityDailyConfigurationUpdate(False, ("changed@example.test",), "")
    if case == "revoked":
        async with owner.begin() as c:
            await c.execute(
                text("UPDATE user_account SET status=0 WHERE id=:id"), {"id": principal.account_id}
            )
    if case == "audit-failure":
        async with owner.begin() as c:
            assert await c.scalar(
                text("SELECT has_table_privilege('sms_accept','audit_log','INSERT')")
            )
            await c.execute(text("REVOKE INSERT ON audit_log FROM sms_accept"))
    before = await daily_snapshot(owner, principal)
    try:

        async def write():
            return await repo.update_configuration(
                update,
                principal=principal,
                ip="127.0.0.1",
                authorization=grant,
                expected_config_version=version,
            )

        if case == "success":
            result = await write()
            assert result.config_version == version + 1
            configs, recipients, audits = await daily_snapshot(owner, principal)
            state = {row["key"]: row["value"] for row in configs}
            assert state["security_daily_config_publish_state"] == "file_pending"
            assert state["security_daily_config_operation_id"]
            assert state["security_daily_config_version"] == str(version + 1)
            assert recipients == [{"position": 1, "address": "synthetic@example.test"}]
            assert audits == before[2] + 2
        else:
            with pytest.raises(SecurityDailyStateConflict if case == "stale" else ApiError):
                await write()
            assert await daily_snapshot(owner, principal) == before
    finally:
        if case == "audit-failure":
            async with owner.begin() as c:
                await c.execute(text("GRANT INSERT ON audit_log TO sms_accept"))
