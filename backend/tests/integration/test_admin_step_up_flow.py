"""真实 JWT、Redis Lua、PostgreSQL 与回环 HTTP 的管理操作授权链。"""

import asyncio
import os
import socket
from ipaddress import ip_network
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text

from app.api import admin, admin_step_up, apps, security_daily
from app.core.audit import insert_audit
from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.jwt import JwtService
from app.core.auth.local import LocalPasswordProvider
from app.core.auth.passwords import LocalPasswordHasher
from app.core.auth.principal_context import bind_audit_principal
from app.core.auth.runtime import get_auth_facade
from app.core.auth.service import RedisKeyValue
from app.core.auth.users import SqlUserRepository
from app.core.correlation import CorrelationIdMiddleware
from app.core.errors import ApiError, api_error_handler
from app.core.runtime_resources import database_engine
from app.services.admin import AdminService
from app.services.admin_repository import SqlAdminRepository
from app.services.admin_step_up import AdminStepUpService, _key, admin_intent
from app.services.app_contract import AppCreateRequest, AppUpdateRequest
from app.services.app_management import AppManagementService, CallbackUrlValidator
from app.services.app_repository import SqlAppRepository
from app.services.security_daily import FileSecurityDailyControl, SecurityDailyService
from tests.integration.test_auth_guard_redis import PrefixedRedisKeyValue
from tests.integration.test_config_authorization_postgres import (  # noqa: F401
    config_context,
    flow_owner,
)
from tests.integration.test_daily_authorization_postgres import daily_context  # noqa: F401
from tests.integration.test_security_session_postgres import claims as jwt_claims
from tests.test_app_management import crypto

pytestmark = pytest.mark.skipif(
    not {"EXPORT_AUTH_POSTGRES_DSN", "AUTH_GUARD_REDIS_URL"}.issubset(os.environ),
    reason="requires isolated PostgreSQL and Redis",
)
PASSWORD = "Synthetic@AdminFlow123"


class ObservedStepUp(AdminStepUpService):
    after_consume = None

    async def consume(self, *args, **kwargs):
        authorization = await super().consume(*args, **kwargs)
        if self.after_consume is not None:
            await self.after_consume()
        return authorization


class GrantStore(PrefixedRedisKeyValue):
    async def set(self, key, value, *, ex):
        return await self.delegate.redis.set(self._key(key), value, ex=ex)


@pytest.fixture
async def flow(config_context, daily_context, tmp_path):  # noqa: F811
    owner, principal, settings, updates, _ = config_context
    async with owner.begin() as c:
        await c.execute(
            text(
                "INSERT INTO local_credential(identity_id,password_hash,must_change_password) "
                "VALUES(:id,:hash,false)"
            ),
            {"id": principal.identity_id, "hash": LocalPasswordHasher().hash(PASSWORD)},
        )
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    store = GrantStore(RedisKeyValue(redis), "admin-flow:" + uuid4().hex)
    users = SqlUserRepository(settings)
    projection = await users.load_security_session(principal.account_id, principal.identity_id)
    tokens = JwtService(
        "synthetic-admin-flow-signing-key-long-enough",
        store,
        security_session_loader=users.load_security_session,
    )
    access = tokens.issue(jwt_claims(projection))

    class HarnessFacade:
        # 使用真实认证原语；登录准入/限速不属于本夹具覆盖范围。
        async def verify(self, token):
            try:
                value = await tokens.verify(token)
            except InvalidCredentials:
                raise ApiError(401, "UNAUTHORIZED", "会话无效", None) from None
            except SessionStateUnavailable:
                raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "会话状态不可用", None) from None
            bind_audit_principal(value.principal)
            return value

        async def reauthenticate_current(self, claims, password, ip):
            identity = await LocalPasswordProvider(users, LocalPasswordHasher()).authenticate(
                claims.login_name,
                password,
                pool="auth_hash",
            )
            if (
                identity.account.account_id != claims.account_id
                or identity.account.security_version != claims.security_version
            ):
                raise ApiError(401, "STEP_UP_REQUIRED", "认证主体变化", None)

    async def audit(event):
        async with database_engine(settings.database_url_for("auth")).begin() as c:
            await insert_audit(c, event)

    facade = HarnessFacade()
    step = ObservedStepUp(facade, store, audit)
    service = AdminService(
        SqlAdminRepository(settings),
        allowed_smtp_hosts={"smtp"},
        callback_egress_networks=(
            ip_network("10.0.0.0/8"),
            ip_network("172.16.0.0/12"),
            ip_network("192.168.0.0/16"),
        ),
    )
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[get_auth_facade] = lambda: facade
    app.dependency_overrides[admin_step_up.get_admin_step_up_service] = lambda: step
    app.dependency_overrides[admin.get_admin_service] = lambda: service
    app_service = AppManagementService(
        SqlAppRepository(settings), crypto(), CallbackUrlValidator("10.0.0.0/8")
    )
    app.dependency_overrides[apps.get_app_management_service] = lambda: app_service
    daily_repo = daily_context[2]
    daily_control = FileSecurityDailyControl(tmp_path / "control", tmp_path / "config")
    daily_service = SecurityDailyService(daily_repo, daily_control)
    app.dependency_overrides[security_daily.get_security_daily_service] = lambda: daily_service
    app.include_router(security_daily.router)
    app.include_router(apps.router)
    app.include_router(admin.router)
    app.include_router(admin_step_up.router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    ready = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    server = ReadyServer(uvicorn.Config(app, access_log=False, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(ready.wait(), 5)
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
        headers={"Authorization": f"Bearer {access}"},
        timeout=10,
        trust_env=False,
    )
    try:
        yield SimpleNamespace(
            client=client,
            step=step,
            redis=redis,
            store=store,
            owner=owner,
            principal=principal,
            settings=settings,
            tokens=tokens,
            access=access,
            revision=updates[0].expected_updated_at.isoformat(),
            daily_repo=daily_repo,
            daily_control=daily_control,
        )
    finally:
        await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
        if store.keys:
            await redis.delete(*store.keys)
        await redis.aclose()
        async with owner.begin() as c:
            await c.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            await c.execute(
                text("DELETE FROM app WHERE created_by=:actor"), {"actor": principal.login_name}
            )
            await c.execute(
                text("DELETE FROM local_credential WHERE identity_id=:id"),
                {"id": principal.identity_id},
            )


def config_items(flow, value="8"):
    return [{"key": "vendor_qps", "value": value, "expected_updated_at": flow.revision}]


async def issue(flow):
    response = await flow.client.post(
        "/api/v1/web/admin/step-up",
        json={
            "operation": "config_update",
            "target_id": "default",
            "parameters": {"items": config_items(flow)},
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["token"]


async def write(flow, token=None, value="8"):
    headers = {"X-Admin-Step-Up": token} if token else {}
    return await flow.client.put(
        "/api/v1/web/admin/configs", headers=headers, json={"items": config_items(flow, value)}
    )


@pytest.mark.asyncio
async def test_real_config_route_requires_grant_and_rejects_replay(flow):
    missing = await write(flow)
    assert missing.status_code == 401
    grant = await issue(flow)
    success = await write(flow, grant)
    assert success.status_code == 200, success.text
    replay = await write(flow, grant)
    assert replay.status_code == 401
    async with flow.owner.connect() as c:
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                    "AND action='config_update'"
                ),
                {"id": flow.principal.account_id},
            )
            == 1
        )


@pytest.mark.asyncio
async def test_real_config_route_concurrent_consumption_commits_once(flow):
    grant = await issue(flow)
    results = await asyncio.gather(write(flow, grant), write(flow, grant))
    assert sorted(r.status_code for r in results) == [200, 401]


@pytest.mark.asyncio
async def test_real_config_route_changed_parameters_cannot_use_grant(flow):
    grant = await issue(flow)
    response = await write(flow, grant, value="9")
    assert response.status_code == 401
    async with flow.owner.connect() as c:
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                    "AND action='config_update'"
                ),
                {"id": flow.principal.account_id},
            )
            == 0
        )


@pytest.mark.asyncio
async def test_real_expired_grant_rejected_without_business_write(flow):
    grant = await issue(flow)
    await flow.redis.expireat(flow.store._key(_key(grant)), 1)
    assert not await flow.redis.exists(flow.store._key(_key(grant)))
    response = await write(flow, grant)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "disabled", "identity", "version"])
async def test_revocation_after_real_consume_before_commit_rejects(flow, change):
    grant = await issue(flow)
    statements = {
        "role": "UPDATE user_account SET role='viewer' WHERE id=:id",
        "disabled": "UPDATE user_account SET status=0 WHERE id=:id",
        "version": "UPDATE user_account SET security_version=security_version+1 WHERE id=:id",
        "identity": "UPDATE auth_identity SET status=0 WHERE id=:id",
    }
    async with flow.owner.connect() as c:
        before = await c.scalar(text("SELECT value FROM sys_config WHERE key='vendor_qps'"))

    async def revoke():
        async with flow.owner.begin() as c:
            await c.execute(
                text(statements[change]),
                {
                    "id": flow.principal.identity_id
                    if change == "identity"
                    else flow.principal.account_id,
                },
            )

    flow.step.after_consume = revoke
    response = await write(flow, grant)
    assert response.status_code == 401
    assert not await flow.redis.exists(flow.store._key(_key(grant)))
    async with flow.owner.connect() as c:
        assert await c.scalar(text("SELECT value FROM sys_config WHERE key='vendor_qps'")) == before
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                    "AND action='config_update'"
                ),
                {"id": flow.principal.account_id},
            )
            == 0
        )


async def issue_operation(flow, operation, target, parameters):
    response = await flow.client.post(
        "/api/v1/web/admin/step-up",
        json={
            "operation": operation,
            "target_id": target,
            "parameters": parameters,
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["create", "update", "disable", "rotate_key", "revoke_old_key", "rotate_callback_secret"],
)
@pytest.mark.parametrize("actor_kind", ["admin", "anonymous", "viewer"])
async def test_real_app_route_matrix(flow, operation, actor_kind):
    config = AppCreateRequest(
        name="route-app-" + uuid4().hex[:10],
        dept="平台部",
        daily_quota=100,
        admission_exempt_note="   ",
    ).model_dump(mode="json")
    base = "/api/v1/web/admin/apps"
    if operation == "create":
        method, path, body = "POST", base, config
        target, parameters, headers = "new", {"config": config}, {}
    else:
        initial = await issue_operation(flow, "app_create", "new", {"config": config})
        created = await flow.client.post(base, json=config, headers={"X-Admin-Step-Up": initial})
        assert created.status_code == 200, created.text
        app_id = created.json()["id"]
        current_response = await flow.client.get(f"{base}/{app_id}")
        assert current_response.status_code == 200, current_response.text
        current = current_response.json()
        target = str(app_id)
        parameters = {"expected_updated_at": current["updated_at"]}
        headers = {"X-App-Revision": current["updated_at"]}
        body = None
        if operation == "update":
            method, path = "PUT", f"{base}/{app_id}"
            body = {key: current[key] for key in AppUpdateRequest.model_fields}
            body["daily_quota"] = 101
            body["admission_exempt_note"] = "\t\n"
            parameters["config"] = body
        elif operation == "disable":
            method, path = "DELETE", f"{base}/{app_id}"
        else:
            method, path = "POST", f"{base}/{app_id}/{operation.replace('_', '-')}"
    if actor_kind == "anonymous":
        flow.client.headers.pop("Authorization")
    elif actor_kind == "viewer":
        async with flow.owner.begin() as c:
            await c.execute(
                text("UPDATE user_account SET role='viewer' WHERE id=:id"),
                {"id": flow.principal.account_id},
            )
        projection = await SqlUserRepository(flow.settings).load_security_session(
            flow.principal.account_id,
            flow.principal.identity_id,
        )
        flow.client.headers["Authorization"] = "Bearer " + flow.tokens.issue(jwt_claims(projection))
    missing = await flow.client.request(method, path, json=body, headers=headers)
    assert missing.status_code == (403 if actor_kind == "viewer" else 401), missing.text
    if actor_kind != "admin":
        async with flow.owner.connect() as c:
            assert (
                await c.scalar(
                    text(
                        "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                        "AND action=:action"
                    ),
                    {"id": flow.principal.account_id, "action": "app_" + operation},
                )
                == 0
            )
        return
    grant = await issue_operation(flow, "app_" + operation, target, parameters)
    headers["X-Admin-Step-Up"] = grant
    result = await flow.client.request(method, path, json=body, headers=headers)
    assert result.status_code == (204 if operation == "disable" else 200), result.text
    if operation in {"create", "rotate_key", "rotate_callback_secret"}:
        assert result.headers["cache-control"] == "no-store"
    replay = await flow.client.request(method, path, json=body, headers=headers)
    assert replay.status_code == 401
    async with flow.owner.connect() as c:
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE actor_account_id=:id AND action=:action"
                ),
                {"id": flow.principal.account_id, "action": "app_" + operation},
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "revoked"])
async def test_real_daily_route_publishes_only_after_authorized_commit(flow, case):
    current = await flow.daily_repo.configuration()
    body = {
        "enabled": False,
        "recipients": ["synthetic@example.test"],
        "resend_api_key": "",
        "expected_config_version": current.config_version,
    }
    path = "/api/v1/web/admin/security-daily/config"
    absent = await flow.client.put(path, json=body)
    assert absent.status_code == 401, absent.text
    assert not flow.daily_control.config_path.exists()
    parameters = {
        "enabled": False,
        "recipients": body["recipients"],
        "api_key_action": "clear",
        "expected_config_version": current.config_version,
    }
    grant = await issue_operation(flow, "security_daily_config_update", "default", parameters)
    if case == "revoked":

        async def revoke():
            async with flow.owner.begin() as c:
                await c.execute(
                    text("UPDATE user_account SET status=0 WHERE id=:id"),
                    {"id": flow.principal.account_id},
                )

        flow.step.after_consume = revoke
    response = await flow.client.put(path, json=body, headers={"X-Admin-Step-Up": grant})
    if case == "success":
        assert response.status_code == 200, response.text
        assert response.json()["config_version"] == current.config_version + 1
        assert await flow.daily_control.published_config_version() == current.config_version + 1
        replay = await flow.client.put(path, json=body, headers={"X-Admin-Step-Up": grant})
        assert replay.status_code == 401
    else:
        assert response.status_code == 401, response.text
        assert not flow.daily_control.config_path.exists()
        assert await flow.daily_repo.configuration() == current


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["ip", "operation", "session"])
async def test_real_grant_binding_mismatches_rejected(flow, mismatch):
    if mismatch == "ip":
        claims = await flow.tokens.verify(flow.access)
        grant = await flow.step.issue(
            claims=claims,
            password=PASSWORD,
            ip="127.0.0.2",
            intent=admin_intent("config_update", "default", {"items": config_items(flow)}),
        )
    elif mismatch == "operation":
        grant = await issue_operation(
            flow, "app_disable", "1", {"expected_updated_at": flow.revision}
        )
    else:
        grant = await issue(flow)
        projection = await SqlUserRepository(flow.settings).load_security_session(
            flow.principal.account_id,
            flow.principal.identity_id,
        )
        replacement = flow.tokens.issue(jwt_claims(projection))
        assert (await flow.tokens.verify(replacement)).jti != (
            await flow.tokens.verify(flow.access)
        ).jti
        flow.client.headers["Authorization"] = "Bearer " + replacement
    response = await write(flow, grant)
    assert response.status_code == 401, response.text
    async with flow.owner.connect() as c:
        assert (
            await c.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                    "AND action='config_update'"
                ),
                {"id": flow.principal.account_id},
            )
            == 0
        )


@pytest.mark.asyncio
async def test_real_redis_connection_failure_closes_write(flow):
    grant = await issue(flow)
    # 独占绑定但不监听的回环端口：真实连接拒绝，不停止共享测试 Redis。
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    unavailable = Redis(
        host="127.0.0.1",
        port=listener.getsockname()[1],
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    original_store = flow.step.store
    try:
        flow.step.store = unavailable
        response = await write(flow, grant)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "AUTH_SESSION_UNAVAILABLE"
        async with flow.owner.connect() as c:
            assert (
                await c.scalar(
                    text(
                        "SELECT count(*) FROM audit_log WHERE actor_account_id=:id "
                        "AND action='config_update'"
                    ),
                    {"id": flow.principal.account_id},
                )
                == 0
            )
    finally:
        flow.step.store = original_store
        await unavailable.aclose()
        listener.close()
