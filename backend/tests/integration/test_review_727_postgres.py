"""审查修复的真实事务与 Redis 竞争回归；只接受隔离测试环境。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
from app.core.auth.users import SqlUserRepository
from app.services.admin_invariant import ensure_effective_admin
from app.services.ops import QueueSnapshot
from app.services.ops_repository import SqlOpsRepository
from app.services.stats_repository import SqlStatsRepository
from app.services.user_management import LastAdminProtected
from tests.integration.test_ops_audit_postgres import send_runtime as send_runtime

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("departments", [(), ("one", "two"), ("one", "one")])
async def test_effective_ad_admin_requires_one_department(departments: tuple[str, ...]) -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("UPDATE user_account SET status=0"))
                provider = await connection.scalar(
                    text(
                        "INSERT INTO auth_provider(code,name,kind,enabled) "
                        "VALUES(:code,'synthetic','ldap',true) RETURNING id"
                    ),
                    {"code": uuid4().hex},
                )
                account = await connection.scalar(
                    text(
                        "INSERT INTO user_account(role,role_override) VALUES('admin',true) "
                        "RETURNING id"
                    )
                )
                groups = [f"group-{i}" for i in range(len(departments))]
                await connection.execute(
                    text(
                        "INSERT INTO auth_identity(account_id,provider_id,login_name,"
                        "normalized_login_name,external_subject,source_groups) "
                        "VALUES(:account,:provider,:login,:login,:login,:groups)"
                    ),
                    {
                        "account": account,
                        "provider": provider,
                        "login": uuid4().hex,
                        "groups": groups,
                    },
                )
                for group, department in zip(groups, departments, strict=True):
                    await connection.execute(
                        text(
                            "INSERT INTO "
                            "external_role_mapping(provider_id,external_group,role,dept) "
                            "VALUES(:provider,:group,'viewer',:dept)"
                        ),
                        {"provider": provider, "group": group, "dept": department},
                    )
                if len(set(departments)) == 1:
                    await ensure_effective_admin(connection)
                else:
                    with pytest.raises(LastAdminProtected):
                        await ensure_effective_admin(connection)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_directory_evidence_cannot_bind_new_configuration() -> None:
    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(url)
    code = "review-" + uuid4().hex[:16]
    repository = SqlUserRepository(SimpleNamespace(database_url_for=lambda _: url))  # type: ignore[arg-type]
    try:
        async with engine.begin() as connection:
            provider = await connection.scalar(
                text(
                    "INSERT INTO auth_provider(code,name,kind,enabled,active_version) "
                    "VALUES(:code,'synthetic','ldap',true,1) RETURNING id"
                ),
                {"code": code},
            )
        identity = AuthenticatedIdentity(
            code, code, code, code, "one", ("one",), provider_id=provider, provider_version=1
        )
        # 模拟 LDAP 已读取 v1 后，管理员提交了 v2；旧证据不得更新账号或审计。
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_provider SET active_version=2 WHERE id=:id"), {"id": provider}
            )
        with pytest.raises(InvalidCredentials):
            await repository._synchronize_external(identity, "192.0.2.1")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM auth_identity WHERE provider_id=:id"),
                    {"id": provider},
                )
                == 0
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM auth_provider WHERE code=:code"), {"code": code}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_dirty_writer_during_aggregation_survives_commit() -> None:
    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    owner = create_async_engine(url)
    stat_date = date(2041, 1, 1)
    deleted = asyncio.Event()
    resume = asyncio.Event()

    class Connection:
        def __init__(self, connection: Any):
            self.connection = connection

        async def execute(self, statement: Any, *args: Any):
            result = await self.connection.execute(statement, *args)
            if str(statement).startswith("DELETE FROM stat_dirty_date"):
                deleted.set()
                await resume.wait()
            return result

    class Engine:
        @asynccontextmanager
        async def begin(self):
            async with owner.begin() as connection:
                yield Connection(connection)

        async def dispose(self):
            pass

    repository = SqlStatsRepository(SimpleNamespace(database_url=url))  # type: ignore[arg-type]
    repository._engine = lambda: Engine()  # type: ignore[method-assign]

    async def dirty():
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO stat_dirty_date(stat_date) VALUES(:day) "
                    "ON CONFLICT (stat_date) DO UPDATE SET created_at=now()"
                ),
                {"day": stat_date},
            )

    try:
        await dirty()
        aggregate = asyncio.create_task(repository.aggregate_day(stat_date))
        await asyncio.wait_for(deleted.wait(), 3)
        writer = asyncio.create_task(dirty())
        await asyncio.sleep(0.05)
        assert not writer.done()
        resume.set()
        await asyncio.wait_for(asyncio.gather(aggregate, writer), 3)
        async with owner.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM stat_dirty_date WHERE stat_date=:day"),
                    {"day": stat_date},
                )
                == 1
            )
        # 下一次正常聚合消费新标记。
        await repository.aggregate_day(stat_date)
        async with owner.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM stat_dirty_date WHERE stat_date=:day"),
                    {"day": stat_date},
                )
                == 0
            )
    finally:
        resume.set()
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM stat_dirty_date WHERE stat_date=:day"), {"day": stat_date}
            )
        await owner.dispose()


@pytest.mark.asyncio
async def test_queue_resume_cas_preserves_new_pause() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"])
    repository = SqlOpsRepository(SimpleNamespace(database_url="unused"), redis=redis)  # type: ignore[arg-type]
    keys = ("queue:paused:realtime", "queue:paused:bulk")
    try:
        await redis.mset(dict.fromkeys(keys, "old-generation"))
        snapshot = QueueSnapshot("old", "old", 1, 0, b"old-generation", b"old-generation")
        await redis.set(keys[1], "new-generation")
        assert not await repository.clear_queue_pauses(snapshot)
        assert await redis.mget(keys) == [b"old-generation", b"new-generation"]
        current = QueueSnapshot("old", "new", 1, 0, b"old-generation", b"new-generation")
        assert await repository.clear_queue_pauses(current)
        assert await redis.mget(keys) == [None, None]
    finally:
        await redis.delete(*keys)
        await redis.aclose()


@pytest.mark.asyncio
async def test_audit_guard_probe_tests_payload_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts_support import check_migration

    statements: list[str] = []
    monkeypatch.setattr(check_migration, "docker_psql", lambda _, __, sql: statements.append(sql))
    check_migration.verify_audit_payload_guard("synthetic", "synthetic")
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.exec_driver_sql(statements[0])
                # 证明探针不会被缺失的 payload 防护误导为通过。
                await connection.execute(
                    text("ALTER TABLE audit_log DROP CONSTRAINT ck_audit_payload_no_pii")
                )
                from sqlalchemy.exc import DBAPIError

                with pytest.raises(DBAPIError, match="accepted forbidden payload"):
                    await connection.exec_driver_sql(statements[0])
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_critical_pause_reassertion_is_not_cleared() -> None:
    import redis as sync_redis

    from tests.test_vendor_test_manager import manager_module

    client = sync_redis.Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    operations = object.__new__(manager_module.HostActivationOperations)

    def command(*args: str) -> str:
        value = client.execute_command(*args)
        return str(value) if value is not None else ""

    operations._redis = command
    operations.stop_senders = lambda: None
    keys = operations._resume_pause_keys()
    try:
        client.delete(*keys)
        operations.hold_fail_closed()
        first = operations.pause_snapshot()
        operations.hold_fail_closed()
        assert operations.current_pause_kind() == "critical"
        with pytest.raises(manager_module.VendorTestActivationError, match="changed"):
            operations.clear_pause("critical", first)
        assert operations.current_pause_kind() == "critical"
        operations.clear_pause("critical", operations.pause_snapshot())
        assert operations.current_pause_kind() is None
    finally:
        client.delete(*keys)
        client.close()


@pytest.mark.asyncio
async def test_approval_waiting_for_lock_cannot_cross_deadline() -> None:
    from app.core.auth.accounts import SecurityPrincipal
    from app.services.approval_repository import SqlApprovalRepository

    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(url)
    repository = SqlApprovalRepository(SimpleNamespace(database_url=url))  # type: ignore[arg-type]
    account = batch = approval = None
    try:
        async with engine.begin() as connection:
            account = await connection.scalar(
                text("INSERT INTO user_account DEFAULT VALUES RETURNING id")
            )
            identity = await connection.scalar(
                text(
                    "INSERT INTO auth_identity(account_id,provider_id,login_name,"
                    "normalized_login_name,external_subject) "
                    "SELECT :account,id,:login,:login,:login FROM auth_provider "
                    "WHERE code='local' RETURNING id"
                ),
                {"account": account, "login": uuid4().hex},
            )
            batch = await connection.scalar(
                text(
                    "INSERT INTO "
                    "sms_batch(batch_no,channel,dept,content,status,display_content_enc,sen"
                    "d_content_enc) "
                    "VALUES(:number,'web','synthetic','[encrypted]','pending_approval',deco"
                    "de('aa','hex'),decode('bb','hex')) RETURNING id"
                ),
                {"number": uuid4().hex},
            )
            approval = await connection.scalar(
                text(
                    "INSERT INTO approval(batch_id,applicant,applicant_account_id,"
                    "applicant_identity_id,dept,expires_at) "
                    "VALUES(:batch,'synthetic',:account,:identity,'synthetic',clock_timestamp()+inter"
                    "val '0.2 seconds') RETURNING id"
                ),
                {"batch": batch, "account": account, "identity": identity},
            )
        async with engine.begin() as lock:
            await lock.execute(
                text("SELECT id FROM approval WHERE id=:id FOR UPDATE"), {"id": approval}
            )
            pending = asyncio.create_task(
                repository.transition(
                    approval,
                    action="approve",
                    reason=None,
                    principal=SecurityPrincipal(
                        account + 1, 999999, "synthetic-reviewer", "synthetic", "admin"
                    ),
                )
            )
            await asyncio.sleep(0.3)
            assert not pending.done()
        assert await asyncio.wait_for(pending, 3) is None
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT status FROM approval WHERE id=:id"), {"id": approval}
                )
                == "pending"
            )
            assert (
                await connection.scalar(
                    text("SELECT status FROM sms_batch WHERE id=:id"), {"id": batch}
                )
                == "pending_approval"
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM approval WHERE id=:id"), {"id": approval})
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch})
            await connection.execute(
                text("DELETE FROM auth_identity WHERE account_id=:id"), {"id": account}
            )
            await connection.execute(text("DELETE FROM user_account WHERE id=:id"), {"id": account})
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0094_preserves_legacy_released_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    module = importlib.import_module("migrations.versions.0094_send_inflight_reservation_lifecycle")
    statements: list[str] = []
    monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
    module.upgrade()
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "ALTER TABLE send_inflight_reservation DROP CONSTRAINT "
                        "ck_inflight_released_pair"
                    )
                )
                app = await connection.scalar(
                    text(
                        "INSERT INTO app(name,dept,api_key_hash,api_key_prefix,created_by) "
                        "VALUES(:name,'synthetic',repeat('a',64),'synthetc','synthetic') "
                        "RETURNING id"
                    ),
                    {"name": uuid4().hex},
                )
                reservation = await connection.scalar(
                    text(
                        "INSERT INTO "
                        "send_inflight_reservation(app_id,reserved_chunks,state,released_at) "
                        "VALUES(:app,1,'released',now()) RETURNING id"
                    ),
                    {"app": app},
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                for statement in statements:
                    await connection.exec_driver_sql(statement)
                result = (
                    await connection.execute(
                        text(
                            "SELECT state,released_at,release_reason FROM "
                            "send_inflight_reservation WHERE id=:id"
                        ),
                        {"id": reservation},
                    )
                ).one()
                assert result.state == "released" and result.released_at is not None
                assert result.release_reason == "legacy_released"
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_automatic_daily_delivery_needs_no_secret_config_access(
    send_runtime: Any,
    tmp_path: Any,
    available: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.security_daily import FileSecurityDailyControl, SecurityDailyService
    from app.services.security_daily_repository import SqlSecurityDailyRepository
    from tests.test_security_daily import payload

    owner, send_url = send_runtime
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "audit_producer_domain", "bulk")
    from app.core import runtime_resources

    bulk_key = bytes.fromhex("55" * 32)
    original_key = runtime_resources._audit_context_key
    monkeypatch.setattr(
        runtime_resources,
        "_audit_context_key",
        lambda name: bulk_key if name == "audit_system_bulk_context_key" else original_key(name),
    )
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_context_signing_key(key_kind,key_material) "
                "VALUES('system:bulk',:key) ON CONFLICT(key_kind) "
                "DO UPDATE SET key_material=EXCLUDED.key_material"
            ),
            {"key": bulk_key},
        )
    repository = SqlSecurityDailyRepository(SimpleNamespace(database_url=send_url))
    control_dir = tmp_path / "control"
    control_dir.mkdir()

    class RestrictedControl(FileSecurityDailyControl):
        async def published_config_version(self):
            raise AssertionError("automatic worker cannot read configuration files")

        async def sync_configuration(self, configuration):
            raise AssertionError("automatic worker cannot publish mail credentials")

    control = RestrictedControl(control_dir, tmp_path / "unavailable-config")
    service = SecurityDailyService(repository, control)
    values = {
        "security_daily_enabled": "true",
        "security_daily_resend_configured": "true",
        "security_daily_recipient_count": "1",
        "security_daily_config_version": "1",
        "security_daily_config_publish_state": "file_committed",
        "security_daily_recipient_set_digest": "a" * 64,
    }
    report_id = None
    async with owner.begin() as connection:
        saved = dict(
            (
                await connection.execute(
                    text("SELECT key,value FROM sys_config WHERE key=ANY(CAST(:keys AS text[]))"),
                    {"keys": list(values)},
                )
            ).all()
        )
        for key, value in values.items():
            await connection.execute(
                text("UPDATE sys_config SET value=:value WHERE key=:key"),
                {"key": key, "value": value},
            )
    try:
        import json

        data = payload()
        async with owner.begin() as connection:
            report_id = await connection.scalar(
                text(
                    "INSERT INTO security_daily_report(report_date,period_start,period_end,status,"
                    "generation_source,generation_status,payload) VALUES('2026-07-15',"
                    "'2026-07-15T00:00:00+08:00','2026-07-15T23:59:59+08:00','normal','auto',"
                    ":status,CAST(:payload AS jsonb)) RETURNING id"
                ),
                {
                    "status": "ready" if available else "unavailable",
                    "payload": json.dumps(data) if available else None,
                },
            )
        request = await service.submit_auto_delivery(date(2026, 7, 15))
        assert request is not None and request.state == "pending"
        assert len(list((control_dir / "requests").glob("*.json"))) == 1
        assert not (control_dir / "resend.json").exists()
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM security_daily_delivery_request WHERE report_id=:id"),
                {"id": report_id},
            )
            await connection.execute(
                text("DELETE FROM security_daily_report WHERE id=:id"), {"id": report_id}
            )
            for key, value in saved.items():
                await connection.execute(
                    text("UPDATE sys_config SET value=:value WHERE key=:key"),
                    {"key": key, "value": value},
                )


@pytest.mark.asyncio
async def test_failed_resend_rechecks_after_source_batch_lock() -> None:
    from app.services.pipeline import AcceptCommitConflict, FailedSourceReference
    from app.services.pipeline_repository import SqlPipelineStore

    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(url)
    number = uuid4().hex
    batch = None
    try:
        async with engine.begin() as connection:
            batch = await connection.scalar(
                text(
                    "INSERT INTO "
                    "sms_batch(batch_no,channel,dept,content,status,display_content_enc,sen"
                    "d_content_enc) "
                    "VALUES(:number,'web','synthetic','[encrypted]','completed',decode('aa'"
                    ",'hex'),decode('bb','hex')) RETURNING id"
                ),
                {"number": number},
            )
            message = (
                await connection.execute(
                    text(
                        "INSERT INTO "
                        "sms_message(batch_id,phone_enc,phone_hmac,phone_mask,status,created_at) "
                        "VALUES(:batch,decode('aa','hex'),repeat('a',64),'199****0001','fai"
                        "led','2026-07-15T12:00:00+08:00') "
                        "RETURNING id,created_at"
                    ),
                    {"batch": batch},
                )
            ).one()
        command = SimpleNamespace(
            resend_of=number,
            principal=None,
            channel="web",
            dept="synthetic",
            category="notice",
            is_test=False,
            app_id=None,
            failed_sources=(FailedSourceReference(message.id, message.created_at, "a" * 64, 1),),
        )
        store = SqlPipelineStore(SimpleNamespace(database_url=url))

        async def attempt():
            async with engine.begin() as connection:
                return await store._insert(connection, command, uuid4().hex)

        async with engine.begin() as report:
            await report.execute(
                text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"), {"id": batch}
            )
            pending = asyncio.create_task(attempt())
            await asyncio.sleep(0.05)
            assert not pending.done()
            await report.execute(
                text("UPDATE sms_message SET status='delivered' WHERE batch_id=:id"), {"id": batch}
            )
        with pytest.raises(AcceptCommitConflict, match="失败消息已变化"):
            await asyncio.wait_for(pending, 3)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM sms_batch WHERE resend_of=:id"), {"id": batch}
                )
                == 0
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sms_message WHERE batch_id=:id"), {"id": batch}
            )
            await connection.execute(text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch})
        await engine.dispose()


@pytest.mark.asyncio
async def test_market_outbox_wait_does_not_exhaust_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from app.services.outbox import OutboxEventSpec
    from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox
    from app.tasks import send
    from tests.integration.outbox_isolation import isolated_outbox
    from tests.test_send_worker import FakeBucket, FakeGateway, FakeStore

    url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    repository = SqlOutboxRepository(SimpleNamespace(database_url=url))
    async with isolated_outbox(url, repository._engine()) as (engine, scoped):
        repository._engine = lambda: scoped
        gateway = FakeGateway(["accepted"])
        chunk = send.ChunkPayload(
            7, 8, "synthetic", ("199" + "0" * 7 + "1",), "通知", "", "", category="market"
        )

        class Store(FakeStore):
            async def load_chunk(self, chunk_id):
                return chunk, "bulk"

        store = Store()
        now = datetime.fromisoformat("2041-01-01T22:00:00+08:00")
        worker = send.SendWorker(gateway, store, FakeBucket(), clock=lambda: now)

        async def components():
            return worker, store, gateway, 1

        async def noop(*args, **kwargs):
            pass

        gateway.aclose = noop
        monkeypatch.setattr(send, "_components", components)
        monkeypatch.setattr(send, "SqlOutboxRepository", lambda: repository)
        monkeypatch.setattr("app.services.runtime_heartbeat.touch_runtime_heartbeat", noop)
        async with scoped.begin() as connection:
            event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    "chunk.ready",
                    "sms_chunk",
                    "7",
                    "app.tasks.send.process_chunk",
                    "bulk",
                    (7,),
                    "chunk.ready:7",
                ),
            )
        for _ in range(13):
            leases = await repository.lease_due(limit=1, lease_seconds=30)
            assert len(leases) == 1
            await repository.mark_published(event_id, leases[0].lease_id)
            await send._process_chunk_event(7, str(event_id))
            async with scoped.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT state,attempts,failure_count,next_attempt_at "
                            "FROM outbox_event WHERE id=:id"
                        ),
                        {"id": event_id},
                    )
                ).one()
            assert (row.state, row.attempts, row.failure_count) == ("pending", 0, 0)
            assert row.next_attempt_at == datetime.fromisoformat("2041-01-02T08:00:00+08:00")
            assert await repository.lease_due(limit=1, lease_seconds=30) == []
            # 加速再次检查策略，不改失败次数；真实消费者链每次重新读取窗口。
            async with scoped.begin() as connection:
                await connection.execute(
                    text("UPDATE outbox_event SET next_attempt_at=now() WHERE id=:id"),
                    {"id": event_id},
                )
        from app.services.outbox import OUTBOX_CAPACITY_ACTIVE_SQL
        async with scoped.connect() as connection:
            await connection.execute(text(
                "UPDATE outbox_event SET next_attempt_at='2041-01-02T08:00:00+08:00'"
            ))
            assert await connection.scalar(text(
                f"SELECT count(*) FROM outbox_event WHERE {OUTBOX_CAPACITY_ACTIVE_SQL}"
            )) == 0
        assert gateway.calls == 0
        now = datetime.fromisoformat("2041-01-02T08:00:00+08:00")
        leases = await repository.lease_due(limit=1, lease_seconds=30)
        await repository.mark_published(event_id, leases[0].lease_id)
        await send._process_chunk_event(7, str(event_id))
        assert gateway.calls == 1
        async with scoped.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT state FROM outbox_event WHERE id=:id"), {"id": event_id}
                )
                == "completed"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_evidence", [False, True])
async def test_migration_0117_downgrade_preserves_acceptance_evidence(
    monkeypatch: pytest.MonkeyPatch, has_evidence: bool,
) -> None:
    import importlib

    from sqlalchemy.exc import DBAPIError

    module = importlib.import_module("migrations.versions.0117_review_acceptance_facts")
    statements: list[str] = []
    monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
    module.downgrade()
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("DELETE FROM vendor_test_operation"))
                await connection.execute(
                    text(
                        "INSERT INTO vendor_test_operation("
                        "id,operation_type,actor,status,lease_expires_at,"
                        "acceptance_reference_required) VALUES("
                        ":id,'uat_send','synthetic','requested',"
                        "now()+interval '1 minute',:required)"
                    ),
                    {"id": uuid4(), "required": has_evidence},
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                if has_evidence:
                    async with connection.begin_nested() as savepoint:
                        with pytest.raises(DBAPIError) as failure:
                            for statement in statements:
                                await connection.exec_driver_sql(statement)
                        assert "acceptance evidence cannot" in str(failure.value.orig)
                        await savepoint.rollback()
                    assert await connection.scalar(
                        text("SELECT count(*) FROM vendor_test_operation "
                             "WHERE acceptance_reference_required")
                    ) == 1
                else:
                    for statement in statements:
                        await connection.exec_driver_sql(statement)
                    assert await connection.scalar(text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name='vendor_test_operation' "
                        "AND column_name='acceptance_reference_required'"
                    )) == 0
                    assert await connection.scalar(
                        text("SELECT count(*) FROM vendor_test_operation")
                    ) == 1
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
