"""真实 Redis、PostgreSQL、加密文件与回环 HTTP 的下载授权组合。"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.api import reports
from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.export_authorization import lock_export_authorization
from app.core.auth.jwt import JwtService
from app.core.auth.local import LocalPasswordProvider
from app.core.auth.passwords import LocalPasswordHasher
from app.core.auth.principal_context import audit_principal_scope, bind_audit_principal
from app.core.auth.runtime import get_auth_facade
from app.core.auth.service import RedisKeyValue
from app.core.auth.users import SqlUserRepository
from app.core.correlation import CorrelationIdMiddleware, correlation_scope
from app.core.errors import ApiError, api_error_handler
from app.core.runtime_resources import _BUDGETS, close_runtime_resources
from app.services.export import ExportFilterSet, ExportService
from app.services.export_file import ExportFileCodec
from app.services.export_repository import SqlExportRepository
from app.services.export_step_up import ExportStepUpService, _key
from tests.integration.test_auth_guard_redis import PrefixedRedisKeyValue
from tests.integration.test_daily_password_cas_postgres import _create_local
from tests.integration.test_security_session_postgres import claims as jwt_claims
from tests.test_export_file import crypto

pytestmark = pytest.mark.skipif(
    not {"EXPORT_AUTH_POSTGRES_DSN", "AUTH_GUARD_REDIS_URL"}.issubset(os.environ),
    reason="requires isolated PostgreSQL and Redis",
)
PASSWORD = "Synthetic@Export123"


class TrackingCodec(ExportFileCodec):
    def __init__(self, crypto, root):
        super().__init__(crypto, root, frame_size=32)
        self.resources = []

    def prepare_download(self, *args, **kwargs):
        resource = super().prepare_download(*args, **kwargs)
        self.resources.append(resource)
        return resource


class ObservedStepUp(ExportStepUpService):
    after_consume = None

    async def consume(self, *args, **kwargs):
        await super().consume(*args, **kwargs)
        if self.after_consume:
            await self.after_consume()


@pytest.fixture
async def flow_owner():
    """即使请求夹具初始化失败，也关闭本用例的 owner 连接池。"""
    assert os.environ.get("SMS_ISOLATED_TEST_DATABASE")
    owner = create_async_engine(
        make_url(os.environ["EXPORT_AUTH_POSTGRES_DSN"]), hide_parameters=True,
    )
    try:
        yield owner
    finally:
        await close_runtime_resources()
        await owner.dispose()


async def set_role_login(connection, role, password, can_login):
    # 密码或原 SCRAM 校验器仅作为隐藏绑定参数传入，不拼到 SQL/日志中。
    await connection.execute(text("""
        SELECT set_config('sms.test_role',:role,TRUE),
               set_config('sms.test_role_password',:password,TRUE)
    """), {"role": role, "password": password or ""})
    mode = "LOGIN" if can_login else "NOLOGIN"
    await connection.execute(text("""
        DO $$ BEGIN
          EXECUTE format('ALTER ROLE %I """ + mode + """ PASSWORD %L',
            current_setting('sms.test_role'),
            NULLIF(current_setting('sms.test_role_password'),''));
        END $$
    """))


@pytest.fixture
async def runtime_roles(flow_owner, monkeypatch):
    """使用真实运行角色，并精确恢复共享测试集群原先的登录状态。"""
    owner = flow_owner
    role_password = secrets.token_hex(24)
    signing_key = secrets.token_bytes(32)
    async with owner.begin() as connection:
        roles = (await connection.execute(text("""
            SELECT rolname,rolcanlogin,rolpassword FROM pg_authid
            WHERE rolname IN ('sms_auth','sms_export')
        """))).all()
        assert len(roles) == 2
        prior_key = await connection.scalar(text("""
            SELECT key_material FROM audit_context_signing_key WHERE key_kind='principal'
        """))
    try:
        async with owner.begin() as connection:
            for role, _, _ in roles:
                await set_role_login(connection, role, role_password, True)
            await connection.execute(text("""
                INSERT INTO audit_context_signing_key(key_kind,key_material)
                VALUES('principal',:key) ON CONFLICT(key_kind)
                DO UPDATE SET key_material=EXCLUDED.key_material
            """), {"key": signing_key})
        monkeypatch.setattr("app.core.runtime_resources._audit_context_key", lambda _: signing_key)
        role_urls = {
            role: owner.url.set(username=f"sms_{role}", password=role_password)
            for role in ("auth", "export")
        }
        yield SimpleNamespace(database_url_for=lambda role: role_urls[role])
    finally:
        await close_runtime_resources()
        async with owner.begin() as connection:
            for role, can_login, password in roles:
                await set_role_login(connection, role, password, can_login)
            if prior_key is None:
                await connection.execute(text(
                    "DELETE FROM audit_context_signing_key WHERE key_kind='principal'"
                ))
            else:
                await connection.execute(text(
                    "UPDATE audit_context_signing_key SET key_material=:key "
                    "WHERE key_kind='principal'"
                ), {"key": prior_key})


@pytest.fixture
async def flow(tmp_path, monkeypatch, flow_owner, runtime_roles):
    # 合法最小池：最终授权不得嵌套申请同池第二条连接。
    monkeypatch.setitem(_BUDGETS, "api", replace(
        _BUDGETS["api"], pool_size=1, max_overflow=0, pool_timeout_seconds=0.2,
    ))
    owner_url = make_url(os.environ["EXPORT_AUTH_POSTGRES_DSN"])
    owner = flow_owner
    owner_settings = SimpleNamespace(database_url_for=lambda _: owner_url)
    login = f"export-flow-{uuid4().hex[:12]}"
    aid, iid, _, _ = await _create_local(owner, login)
    async with owner.begin() as connection:
        await connection.execute(text("UPDATE user_account SET role='approver' WHERE id=:id"),
                                 {"id": aid})
        await connection.execute(
            text("UPDATE local_credential SET password_hash=:hash WHERE identity_id=:id"),
            {"hash": LocalPasswordHasher().hash(PASSWORD), "id": iid},
        )
    initial_users = SqlUserRepository(owner_settings)
    projection = await initial_users.load_security_session(aid, iid)
    initial_claims = jwt_claims(projection)
    codec = TrackingCodec(crypto(), tmp_path)
    initial_repository = SqlExportRepository(owner_settings)
    with correlation_scope(uuid4()), audit_principal_scope(initial_claims.principal):
        task = await initial_repository.create(
            principal=initial_claims.principal,
            filters=ExportFilterSet(None, None, None, None, None, None, (), "测试部"),
            decrypted=True,
        )

    async def rows():
        yield ("synthetic record", "delivered")

    path = await codec.write_csv(task.id, uuid4(), ("record", "status"), rows())
    async with owner.begin() as connection:
        await connection.execute(text("""
            UPDATE export_task SET status='done',file_path=:path,row_count=1,finished_at=now()
            WHERE id=:id
        """), {"id": task.id, "path": str(path)})

    settings = runtime_roles
    users = SqlUserRepository(settings)
    repository = SqlExportRepository(settings)
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    store = PrefixedRedisKeyValue(RedisKeyValue(redis), f"export-flow:{uuid4().hex}")
    tokens = JwtService("synthetic-export-flow-signing-key-long-enough", store,
                        security_session_loader=users.load_security_session)
    access = tokens.issue(initial_claims)
    token_claims = await tokens.verify(access)

    class Facade:
        async def verify(self, token, **kwargs):
            try:
                value = await tokens.verify(token, **kwargs)
            except InvalidCredentials:
                raise ApiError(401, "UNAUTHORIZED", "登录会话已失效", None) from None
            except SessionStateUnavailable:
                raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "会话状态不可用", None) from None
            bind_audit_principal(value.principal)
            return value

        async def reauthenticate_current(self, claims, password, ip):
            identity = await LocalPasswordProvider(users, LocalPasswordHasher()).authenticate(
                claims.login_name, password, pool="auth_hash",
            )
            assert identity.account.account_id == claims.account_id
            assert identity.account.security_version == claims.security_version

    facade = Facade()
    step = ObservedStepUp(facade, store)
    service = ExportService(repository, codec.crypto, retention_days=7)
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[get_auth_facade] = lambda: facade
    app.dependency_overrides[reports.get_export_service] = lambda: service
    app.dependency_overrides[reports.get_export_codec] = lambda: codec
    app.dependency_overrides[reports.get_export_step_up_service] = lambda: step
    app.include_router(reports.router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    ready = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    server = ReadyServer(uvicorn.Config(
        app, access_log=False, log_level="error", lifespan="off",
    ))
    serve = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(ready.wait(), 5)
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {access}"},
        timeout=10, trust_env=False,
    )
    context = SimpleNamespace(
        owner=owner, users=users, repository=repository, codec=codec, path=path, task=task,
        step=step, tokens=tokens, claims=token_claims, access=access, redis=redis, store=store,
        client=client, account_id=aid, identity_id=iid, trigger_name=None,
    )
    try:
        yield context
    finally:
        await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(serve, 5)
        listener.close()
        if store.keys:
            await redis.delete(*store.keys)
        await redis.aclose()
        await close_runtime_resources()
        async with owner.begin() as connection:
            if context.trigger_name:
                await connection.execute(text(f"DROP TRIGGER {context.trigger_name} ON audit_log"))
                await connection.execute(text(f"DROP FUNCTION {context.trigger_name}()"))



def url(flow, suffix="download"):
    return f"/api/v1/web/reports/export/{flow.task.public_id}/{suffix}"


async def issue(flow):
    response = await flow.client.post(url(flow, "step-up"), json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def remaining(flow, token):
    return await flow.redis.exists(flow.store._key(_key(token)))


async def audit_count(flow):
    async with flow.owner.connect() as connection:
        return await connection.scalar(text("""
            SELECT count(*) FROM audit_log WHERE action='export_download' AND object_id=:id
        """), {"id": str(flow.task.public_id)})


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_connection", ["auth", "business"])
async def test_connection_loss_before_download_commit_rolls_back_audit(
    flow, monkeypatch, failed_connection,
):
    """最终认证成功后连接丢失：无审计提交、无 CSV、令牌不恢复。"""
    token = await issue(flow)

    @asynccontextmanager
    async def interrupted(*args):
        async with lock_export_authorization(*args) as auth_connection:
            yield auth_connection
            connection = auth_connection if failed_connection == "auth" else args[2]
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            async with flow.owner.begin() as fault:
                assert await fault.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                )

    monkeypatch.setattr("app.services.export_repository.lock_export_authorization", interrupted)
    response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
    assert response.status_code == 503
    assert await audit_count(flow) == 0
    assert await remaining(flow, token) == 0
    assert not response.content.startswith(b"\xef\xbb\xbf")
    assert flow.codec.resources and all(item.closed for item in flow.codec.resources)


@pytest.mark.asyncio
async def test_real_download_has_one_atomic_token_winner_and_one_start_audit(flow):
    token = await issue(flow)
    responses = await asyncio.gather(*[
        flow.client.get(url(flow), headers={"X-Export-Step-Up": token}) for _ in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [200, 401]
    response = next(response for response in responses if response.status_code == 200)
    assert response.content.endswith(b"synthetic record,delivered\r\n")
    assert response.headers["cache-control"] == "no-store"
    assert await remaining(flow, token) == 0 and await audit_count(flow) == 1
    assert flow.codec.resources and all(item.closed for item in flow.codec.resources)


@pytest.mark.parametrize("invalid", [
    "pending", "expired", "missing", "task", "header", "truncated", "tag", "symlink",
])
@pytest.mark.asyncio
async def test_known_invalid_download_does_not_consume_or_audit(flow, invalid):
    token = await issue(flow)
    if invalid in {"pending", "expired", "task"}:
        change = {"pending": "status='pending'", "expired": "finished_at=now()-interval '8 days'",
                  "task": "file_path=replace(file_path,'export-' || id || '-', 'export-999999-')"}
        change = change[invalid]
        async with flow.owner.begin() as connection:
            await connection.execute(text(f"UPDATE export_task SET {change} WHERE id=:id"),
                                     {"id": flow.task.id})
    elif invalid == "missing":
        flow.path.unlink()
    elif invalid == "header":
        flow.path.write_bytes(b"invalid")
    elif invalid == "truncated":
        flow.path.write_bytes(flow.path.read_bytes()[:-3])
    elif invalid == "tag":
        payload = bytearray(flow.path.read_bytes())
        payload[20] ^= 1
        flow.path.write_bytes(payload)
    else:
        other = flow.path.with_suffix(".backup")
        flow.path.rename(other)
        flow.path.symlink_to(other)
    response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
    assert response.status_code == 404
    assert await remaining(flow, token) == 1 and await audit_count(flow) == 0
    assert all(item.closed for item in flow.codec.resources)


@pytest.mark.parametrize("change", [
    "disable", "department", "identity", "provider", "expired", "path", "replace", "logout",
])
@pytest.mark.asyncio
async def test_change_after_consume_fails_closed_without_refund_or_audit(flow, change):
    token = await issue(flow)

    async def mutate():
        if change == "replace":
            replacement = flow.path.with_suffix(".replacement")
            replacement.write_bytes(flow.path.read_bytes())
            os.replace(replacement, flow.path)
        elif change == "logout":
            await flow.tokens.revoke_token(flow.access)
        else:
            sql = {
                "disable": "UPDATE user_account SET status=0 WHERE id=:aid",
                "department": "UPDATE user_account SET dept='其他部门' WHERE id=:aid",
                "identity": "UPDATE auth_identity SET status=0 WHERE id=:iid",
                "provider": "UPDATE auth_provider SET enabled=FALSE WHERE code='local'",
                "expired": """UPDATE export_task SET finished_at=now()-interval '8 days'
                    WHERE id=:id""",
                "path": "UPDATE export_task SET file_path='/synthetic/replaced.smsx' WHERE id=:id",
            }[change]
            async with flow.owner.begin() as connection:
                await connection.execute(text(sql), {
                    "aid": flow.account_id, "iid": flow.identity_id, "id": flow.task.id,
                })

    flow.step.after_consume = mutate
    try:
        response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
        assert response.status_code in {401, 404}, response.text
        assert await remaining(flow, token) == 0 and await audit_count(flow) == 0
        assert flow.codec.resources and all(item.closed for item in flow.codec.resources)
        assert not response.content.startswith(b"\xef\xbb\xbf")
    finally:
        if change == "provider":
            async with flow.owner.begin() as connection:
                await connection.execute(text(
                    "UPDATE auth_provider SET enabled=TRUE WHERE code='local'"
                ))


@pytest.mark.parametrize("failure", ["query", "audit"])
@pytest.mark.asyncio
async def test_real_database_failure_after_consume_has_no_csv_and_no_refund(
    flow, monkeypatch, failure,
):
    token = await issue(flow)
    if failure == "query":
        original = flow.repository._downloadable_row

        async def query(connection, *args, **kwargs):
            row = await original(connection, *args, **kwargs)
            if kwargs["locked"]:
                await connection.execute(text("SELECT 1/0"))
            return row

        monkeypatch.setattr(flow.repository, "_downloadable_row", query)
    else:
        name = f"test_export_audit_{uuid4().hex}"
        flow.trigger_name = name
        async with flow.owner.begin() as connection:
            await connection.execute(text(f"""
                CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.action='export_download' AND NEW.object_id='{flow.task.public_id}' THEN
                    RAISE EXCEPTION 'synthetic audit failure' USING ERRCODE='23514';
                  END IF;
                  RETURN NEW;
                END $$
            """))
            await connection.execute(text(f"""
                CREATE TRIGGER {name} BEFORE INSERT ON audit_log
                FOR EACH ROW EXECUTE FUNCTION {name}()
            """))
    response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
    assert response.status_code == 503, response.text
    assert await remaining(flow, token) == 0 and await audit_count(flow) == 0
    assert flow.codec.resources and all(item.closed for item in flow.codec.resources)
    assert "synthetic audit failure" not in response.text
    assert "division" not in response.text


@pytest.mark.parametrize("invalid", ["missing", "expired", "target", "ip", "session"])
@pytest.mark.asyncio
async def test_invalid_step_up_never_authorizes_stream(flow, invalid):
    token = await issue(flow)
    headers = {"X-Export-Step-Up": token}
    if invalid == "missing":
        headers = {}
    elif invalid == "expired":
        await flow.redis.delete(flow.store._key(_key(token)))
    elif invalid in {"target", "ip"}:
        token = await flow.step.issue(
            claims=flow.claims, password=PASSWORD,
            ip="127.0.0.2" if invalid == "ip" else "127.0.0.1",
            public_id=uuid4() if invalid == "target" else flow.task.public_id,
        )
        headers = {"X-Export-Step-Up": token}
    else:
        snapshot = await flow.users.load_security_session(flow.account_id, flow.identity_id)
        flow.client.headers["Authorization"] = f"Bearer {flow.tokens.issue(jwt_claims(snapshot))}"
    response = await flow.client.get(url(flow), headers=headers)
    assert response.status_code == 401, response.text
    assert await audit_count(flow) == 0
    assert all(item.closed for item in flow.codec.resources)


@pytest.mark.asyncio
async def test_new_department_scope_rejects_before_consumption(flow):
    token = await issue(flow)
    async with flow.owner.begin() as connection:
        await connection.execute(text("UPDATE user_account SET dept='其他部门' WHERE id=:id"),
                                 {"id": flow.account_id})
    snapshot = await flow.users.load_security_session(flow.account_id, flow.identity_id)
    flow.client.headers["Authorization"] = f"Bearer {flow.tokens.issue(jwt_claims(snapshot))}"
    response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
    assert response.status_code == 404
    assert await remaining(flow, token) == 1 and await audit_count(flow) == 0
    assert not flow.codec.resources


@pytest.mark.asyncio
async def test_masked_export_remains_available_without_step_up(flow):
    async with flow.owner.begin() as connection:
        await connection.execute(text("UPDATE user_account SET role='viewer' WHERE id=:id"),
                                 {"id": flow.account_id})
        await connection.execute(text("UPDATE export_task SET decrypted=FALSE WHERE id=:id"),
                                 {"id": flow.task.id})
    snapshot = await flow.users.load_security_session(flow.account_id, flow.identity_id)
    flow.client.headers["Authorization"] = f"Bearer {flow.tokens.issue(jwt_claims(snapshot))}"
    response = await flow.client.get(url(flow))
    assert response.status_code == 200 and await audit_count(flow) == 1
    assert all(item.closed for item in flow.codec.resources)


@pytest.mark.asyncio
async def test_expiry_while_waiting_for_export_row_lock_is_rechecked(flow):
    token = await issue(flow)
    async with flow.owner.begin() as connection:
        await connection.execute(text("""
            UPDATE export_task SET finished_at=clock_timestamp()
              -interval '7 days'+interval '1 second'
            WHERE id=:id
        """), {"id": flow.task.id})
    held = await flow.owner.connect()
    transaction = await held.begin()
    ready = asyncio.Event()

    async def hold():
        await held.execute(text("SELECT id FROM export_task WHERE id=:id FOR UPDATE"),
                           {"id": flow.task.id})
        ready.set()

    flow.step.after_consume = hold
    request = asyncio.create_task(flow.client.get(url(flow), headers={"X-Export-Step-Up": token}))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        # 真实锁等待确认，不以两个 coroutine 同时启动冒充并发证据。
        for _ in range(100):
            async with flow.owner.connect() as connection:
                waiting = await connection.scalar(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_locks
                      WHERE locktype='transactionid' AND NOT granted)
                """))
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting
        await asyncio.sleep(1.1)
        await transaction.commit()
        response = await request
        assert response.status_code == 404
        assert await remaining(flow, token) == 0 and await audit_count(flow) == 0
        assert all(item.closed for item in flow.codec.resources)
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await held.close()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 10])
@pytest.mark.parametrize("revoke", [False, True])
async def test_export_checks_out_auth_before_lock_under_real_pool_pressure(
    flow, monkeypatch, capacity, revoke,
):
    """真实满池写事务先完成；等待后的导出仍拒绝已经撤销的主体。"""
    from app.services.admin_invariant import lock_admin_invariant

    token = await issue(flow)
    pool = create_async_engine(
        flow.repository.settings.database_url_for("auth"), pool_size=capacity, max_overflow=0,
        pool_timeout=0.3, connect_args={"server_settings": {"statement_timeout": "5000"}},
    )
    requested = asyncio.Event()
    checked_out = [asyncio.Event() for _ in range(capacity)]
    revocation_committed = asyncio.Event()

    class Managed:
        @asynccontextmanager
        async def begin(self):
            requested.set()
            async with pool.begin() as connection:
                yield connection

        async def dispose(self):
            pass

    monkeypatch.setattr("app.core.auth.export_authorization.database_engine", lambda _: Managed())

    async def writer(index):
        async with pool.begin() as connection:
            checked_out[index].set()
            await requested.wait()
            if revoke and index != 0:
                await revocation_committed.wait()
            await lock_admin_invariant(connection)
            if revoke and index == 0:
                await connection.execute(text(
                    "UPDATE user_account SET security_version=security_version+1 WHERE id=:id"
                ), {"id": flow.account_id})
        if revoke and index == 0:
            revocation_committed.set()

    writers = [asyncio.create_task(writer(i)) for i in range(capacity)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in checked_out)), 5)
        assert pool.pool.checkedout() == capacity
        response = await flow.client.get(url(flow), headers={"X-Export-Step-Up": token})
        await asyncio.wait_for(asyncio.gather(*writers), 5)
        assert response.status_code == (401 if revoke else 200), response.text
        assert await audit_count(flow) == (0 if revoke else 1)
        assert response.content.startswith(b"\xef\xbb\xbf") is not revoke
        assert await remaining(flow, token) == 0
        assert pool.pool.checkedout() == 0
    finally:
        for task in writers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*writers, return_exceptions=True)
        await pool.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_export_waiting_for_auth_pool_does_not_hold_lock_and_releases_on_failure(
    flow, monkeypatch, cancel,
):
    """池耗尽或取消不取得业务锁，也不遗留连接或审计。"""
    from app.core.auth.export_authorization import ExportDownloadAuthorization

    pool = create_async_engine(
        flow.repository.settings.database_url_for("auth"), pool_size=1, max_overflow=0,
        pool_timeout=0.3,
    )
    requested = asyncio.Event()

    class Managed:
        @asynccontextmanager
        async def begin(self):
            requested.set()
            async with pool.begin() as connection:
                yield connection

        async def dispose(self):
            pass

    monkeypatch.setattr("app.core.auth.export_authorization.database_engine", lambda _: Managed())

    async def authorize():
        async with flow.owner.begin() as business, lock_export_authorization(
            flow.repository.settings, ExportDownloadAuthorization.from_claims(flow.claims),
            business,
        ):
            pytest.fail("saturated auth pool must not grant download")

    task = None
    try:
        async with pool.begin():
            task = asyncio.create_task(authorize())
            await asyncio.wait_for(requested.wait(), 5)
            async with flow.owner.begin() as observer:
                assert await observer.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(783692261159983410::bigint)"
                ))
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ApiError) as failure:
                    await asyncio.wait_for(task, 2)
                assert failure.value.code == "AUTH_SESSION_UNAVAILABLE"
                assert failure.value.status_code == 503
        assert pool.pool.checkedout() == 0
        assert await audit_count(flow) == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pool.dispose()


@pytest.mark.asyncio
async def test_export_business_lock_survives_auth_exit_until_audit_commit(flow, monkeypatch):
    """认证事务退出后，业务事务提交前的撤销仍等待同一授权锁。"""
    from app.services.admin_invariant import lock_admin_invariant

    token = await issue(flow)
    auth_exited = asyncio.Event()
    commit_allowed = asyncio.Event()
    writer_started = asyncio.Event()
    writer_pid = None
    seen_audits = None

    @asynccontextmanager
    async def pause_before_commit(*args):
        async with lock_export_authorization(*args) as auth_connection:
            yield auth_connection
        auth_exited.set()
        await commit_allowed.wait()

    monkeypatch.setattr(
        "app.services.export_repository.lock_export_authorization", pause_before_commit,
    )

    async def revoke():
        nonlocal writer_pid, seen_audits
        async with flow.owner.begin() as connection:
            writer_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            writer_started.set()
            await lock_admin_invariant(connection)
            seen_audits = await connection.scalar(text(
                "SELECT count(*) FROM audit_log WHERE action='export_download' AND object_id=:id"
            ), {"id": str(flow.task.public_id)})
            await connection.execute(text(
                "UPDATE user_account SET security_version=security_version+1 WHERE id=:id"
            ), {"id": flow.account_id})

    download = asyncio.create_task(
        flow.client.get(url(flow), headers={"X-Export-Step-Up": token}),
    )
    writer = None
    try:
        await asyncio.wait_for(auth_exited.wait(), 5)
        assert await audit_count(flow) == 0
        writer = asyncio.create_task(revoke())
        await asyncio.wait_for(writer_started.wait(), 5)
        async with flow.owner.connect() as observer:
            for _ in range(100):
                waiting = await observer.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid "
                    "AND locktype='advisory' AND NOT granted)"
                ), {"pid": writer_pid})
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("revocation did not wait on the business authorization lock")
        assert not writer.done()
        commit_allowed.set()
        response = await asyncio.wait_for(download, 5)
        await asyncio.wait_for(writer, 5)
        assert response.status_code == 200, response.text
        assert seen_audits == 1 and await audit_count(flow) == 1
    finally:
        commit_allowed.set()
        for task in (download, writer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (download, writer) if task), return_exceptions=True)
