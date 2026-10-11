"""真实事务拒绝非法部门状态，保留历史坏状态的禁用入口。"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.department import InvalidAccountDepartment
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.admin_step_up import admin_intent
from app.services.user_repository import SqlUserManagementRepository
from tests.integration.test_admin_write_authorization_postgres import (
    context as context,
)
from tests.integration.test_admin_write_authorization_postgres import (
    pytestmark as pytestmark,
)


def authorization(principal, operation, target, parameters=None):
    return AdminAuthorization(
        principal.account_id,
        principal.identity_id,
        1,
        "local",
        None if operation == "user_create" else admin_intent(operation, str(target), parameters),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_empty_department_demotion_is_atomic(context, role):
    engine, principal, target, _, settings = context
    repository = SqlUserManagementRepository(settings)
    async with engine.connect() as connection:
        before = (
            await connection.execute(
                text("SELECT role,dept,security_version FROM user_account WHERE id=:id"),
                {"id": target},
            )
        ).one()
    with (
        audit_principal_scope(principal),
        correlation_scope(uuid4()),
        pytest.raises(InvalidAccountDepartment),
    ):
        await repository.set_role(
            target,
            role,
            True,
            authorization=authorization(principal, "user_role_change", target,
                                        {"role": role, "role_override": True}),
            actor=principal.login_name,
            ip="127.0.0.1",
        )
    async with engine.connect() as connection:
        after = (
            await connection.execute(
                text("SELECT role,dept,security_version FROM user_account WHERE id=:id"),
                {"id": target},
            )
        ).one()
        assert after == before
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_historical_invalid_department_can_disable_but_not_enable(context, role):
    engine, principal, target, _, settings = context
    repository = SqlUserManagementRepository(settings)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE user_account SET role=:role,dept=:dept WHERE id=:id"),
            {"id": target, "role": role, "dept": "\u3000"},
        )
    kwargs = dict(
        actor_account_id=principal.account_id,
        authorization=authorization(principal, "user_status_change", target, {"status": 0}),
        actor=principal.login_name,
        ip="127.0.0.1",
    )
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        assert (await repository.set_status(target, 0, **kwargs)).status == 0
        kwargs["authorization"] = authorization(
            principal, "user_status_change", target, {"status": 1},
        )
        with pytest.raises(InvalidAccountDepartment):
            await repository.set_status(target, 1, **kwargs)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT status FROM user_account WHERE id=:id"), {"id": target}
            )
            == 0
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_invalid_creation_has_no_persisted_account(context, role):
    engine, principal, _, name, settings = context
    repository = SqlUserManagementRepository(settings)
    with (
        audit_principal_scope(principal),
        correlation_scope(uuid4()),
        pytest.raises(InvalidAccountDepartment),
    ):
        await repository.create_local(
            username=name + "new",
            display_name=name,
            dept="\t",
            role=role,
            password_hash="synthetic-hash",
            authorization=authorization(principal, "user_create", "new"),
            actor=principal.login_name,
            ip="127.0.0.1",
        )
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM auth_identity WHERE normalized_login_name=:name"),
                {"name": name + "new"},
            )
            == 0
        )
