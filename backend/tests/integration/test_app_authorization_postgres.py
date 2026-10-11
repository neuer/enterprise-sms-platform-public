"""真实 auth/accept 角色的应用管理授权及版本边界。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent
from app.services.app_contract import AppCreateRequest, AppUpdateRequest
from app.services.app_repository import SqlAppRepository
from tests.integration.test_config_authorization_postgres import (  # noqa: F401
    config_context,
    flow_owner,
    pytestmark,
)


def app_grant(principal, operation, target, parameters):
    return AdminAuthorization(
        principal.account_id,
        principal.identity_id,
        1,
        "local",
        admin_intent(operation, target, parameters),
    )


@pytest.fixture
async def app_context(config_context):  # noqa: F811
    owner, principal, settings, _, _ = config_context
    config = AppCreateRequest(
        name="auth-app-" + uuid4().hex[:10], dept="平台部", daily_quota=100
    ).model_dump()
    values = config | {
        "allowed_categories": ",".join(config["allowed_categories"]),
        "api_key_hash": "a" * 64,
        "api_key_prefix": "test-app",
        "callback_secret_enc": None,
        "actor": principal.login_name,
        "ip": "127.0.0.1",
    }
    repo = SqlAppRepository(settings)
    grant = app_grant(principal, "app_create", "new", {"config": config})
    app_id = None
    try:
        with audit_principal_scope(principal), correlation_scope(uuid4()):
            app_id = await repo.create(authorization=grant, **values)
            yield owner, principal, repo, app_id
    finally:
        if app_id is not None:
            async with owner.begin() as c:
                await c.execute(
                    text("DELETE FROM audit_log WHERE actor_account_id=:id"),
                    {"id": principal.account_id},
                )
                await c.execute(text("DELETE FROM app WHERE id=:id"), {"id": app_id})


async def app_snapshot(owner, principal, app_id):
    async with owner.connect() as c:
        app = (await c.execute(text("SELECT * FROM app WHERE id=:id"), {"id": app_id})).one()
        audits = await c.scalar(
            text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
            {"id": principal.account_id},
        )
        return tuple(app), audits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["update", "disable", "rotate_key", "revoke_old_key", "rotate_callback_secret"]
)
@pytest.mark.parametrize("case", ["success", "stale", "wrong-target", "revoked"])
async def test_app_write_matrix(app_context, operation, case):
    owner, principal, repo, app_id = app_context
    current = await repo.get(app_id)
    revision = current["updated_at"]
    if case == "stale":
        revision -= timedelta(seconds=1)
    parameters = {"expected_updated_at": revision}
    values = {}
    if operation == "update":
        values = {key: current[key] for key in AppUpdateRequest.model_fields}
        values["daily_quota"] = 101
        parameters["config"] = values.copy()
        values["allowed_categories"] = ",".join(values["allowed_categories"])
    elif operation == "rotate_key":
        values = {
            "api_key_hash": "b" * 64,
            "api_key_prefix": "rotated0",
            "old_key_expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    elif operation == "rotate_callback_secret":
        values = {"callback_secret_enc": b"synthetic-encrypted-callback"}
    grant = app_grant(
        principal, "app_" + operation, str(app_id + (case == "wrong-target")), parameters
    )
    if case == "revoked":
        async with owner.begin() as c:
            await c.execute(
                text("UPDATE user_account SET security_version=security_version+1 WHERE id=:id"),
                {"id": principal.account_id},
            )
    before = await app_snapshot(owner, principal, app_id)

    async def write():
        await getattr(repo, operation)(
            app_id,
            authorization=grant,
            expected_updated_at=revision,
            actor=principal.login_name,
            ip="127.0.0.1",
            **values,
        )

    if case == "success":
        await write()
        after = await app_snapshot(owner, principal, app_id)
        assert after[1] == before[1] + 1
        assert after[0] != before[0]
        current = await repo.get(app_id)
        if operation == "update":
            assert current["daily_quota"] == 101
        elif operation == "disable":
            assert current["status"] == 0
        elif operation == "rotate_key":
            assert current["api_key_prefix"] == "rotated0"
        elif operation == "rotate_callback_secret":
            assert current["callback_secret_configured"] is True
        else:
            assert current["old_key_prefix"] is None
    else:
        with pytest.raises(ApiError) as error:
            await write()
        assert error.value.status_code == (409 if case == "stale" else 401)
        assert await app_snapshot(owner, principal, app_id) == before
