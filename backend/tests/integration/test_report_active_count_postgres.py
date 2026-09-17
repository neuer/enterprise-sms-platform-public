"""真实回执路径验证活动计数的首次初始化及旧快照下的后续常量 SQL 工作。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import app.services.report_repository as report_repository_module
from app.services.report_ingest import ProtectedReport
from app.services.report_repository import SqlReportRepository
from tests.test_migration_baseline import load_baseline
from tests.test_report_timeout_sweep import (
    CREATED_AT,
    MINIMAL_SCHEMA,
    EngineBoundRepository,
    _crypto,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated PostgreSQL",
)
ROOT = Path(__file__).resolve().parents[3]


@dataclass
class RefreshCall:
    """只记录真实批次刷新 SQL，独立事实断言和消息定位不进入此范围。"""

    statements: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def message_statements(self) -> list[tuple[str, Any]]:
        return [
            item for item in self.statements
            if re.search(r"\bsms_message\b", item[0], re.IGNORECASE)
        ]


@dataclass
class ActiveCountEnvironment:
    engine: AsyncEngine
    repository: EngineBoundRepository
    calls: list[RefreshCall]


@pytest.fixture
async def active_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ActiveCountEnvironment]:
    """本机独立 schema 仅含合成数据；复用回执测试的最小业务表。"""

    assert os.environ.get("ENVIRONMENT") == "test"
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    assert url.host in {"127.0.0.1", "localhost"}, "requires loopback PostgreSQL"
    schema = f"report_active_{uuid4().hex}"
    owner = create_async_engine(url, hide_parameters=True)
    async with owner.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    calls: list[RefreshCall] = []
    original_refresh = SqlReportRepository._refresh_batch.__func__

    class RecordingConnection:
        def __init__(self, connection: Any, call: RefreshCall) -> None:
            self.connection = connection
            self.call = call

        async def execute(self, statement: Any, parameters: Any = None) -> Any:
            self.call.statements.append((str(statement), parameters))
            return await self.connection.execute(statement, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    async def record_refresh(
        cls: type[SqlReportRepository], connection: Any, batch_id: int, **kwargs: Any,
    ) -> None:
        call = RefreshCall()
        calls.append(call)
        await original_refresh(
            cls, cast(Any, RecordingConnection(connection, call)), batch_id, **kwargs,
        )

    async def no_callback_or_release(*_args: Any, **_kwargs: Any) -> None:
        return None

    # 回调/容量副作用已有独立状态机测试；本例执行实际回执、投影、批次 SQL。
    monkeypatch.setattr(SqlReportRepository, "_refresh_batch", classmethod(record_refresh))
    monkeypatch.setattr(report_repository_module, "enqueue_batch_finished", no_callback_or_release)
    monkeypatch.setattr(report_repository_module, "enqueue_message_report", no_callback_or_release)
    monkeypatch.setattr(
        "app.services.send_inflight.request_inflight_release_for_batch", no_callback_or_release,
    )
    try:
        async with engine.begin() as connection:
            for statement in MINIMAL_SCHEMA.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
            baseline = await asyncio.to_thread(load_baseline)
            schema_source = await asyncio.to_thread((ROOT / "schema.sql").read_text)
            trigger_ddl = [
                statement for statement in baseline.split_sql_statements(schema_source)
                if "invalidate_legacy_batch_active_count" in statement
            ]
            assert len(trigger_ddl) == 3, "复用规范 schema 的函数、权限和触发器，禁止测试假实现"
            for statement in trigger_ddl:
                await connection.execute(baseline.RawSchemaDDL(statement))
            # 仅该合成表关闭自动清理，使 sent 旧版本在测试结束前始终可复现。
            await connection.execute(text("ALTER TABLE sms_message SET (autovacuum_enabled=false)"))
            await connection.execute(text("CREATE INDEX idx_msg_batch ON sms_message(batch_id)"))
            await connection.execute(text(
                "CREATE INDEX idx_msg_active ON sms_message(status) "
                "WHERE status IN ('pending','sent')"
            ))
            await connection.execute(text(
                "CREATE INDEX idx_msg_report_match ON sms_message(chunk_id,phone_hmac)"
            ))
        yield ActiveCountEnvironment(engine, EngineBoundRepository(engine), calls)
    finally:
        await engine.dispose()
        async with owner.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


async def seed_history(engine: AsyncEngine, size: int) -> list[ProtectedReport]:
    """先产生真实 sent 索引历史，仅前三条准备后续回执。"""

    crypto = _crypto()
    filler = crypto.protect_phone("13900000009")
    reports: list[ProtectedReport] = []
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO sms_batch(id,batch_no,status) VALUES(1,'active-count-history','sending')"
        ))
        assert await connection.scalar(text(
            "SELECT active_message_count IS NULL FROM sms_batch WHERE id=1"
        ))
        await connection.execute(text(
            "INSERT INTO sms_chunk(id,batch_id,custom_id,submitted_at,status) "
            "SELECT n,1,'active-count-'||n,now(),'submitted' FROM generate_series(1,:chunks) n"
        ), {"chunks": (size + 499) // 500})
        await connection.execute(text(
            "INSERT INTO sms_message(id,batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,"
            "key_version,status,created_at) "
            "SELECT n,1,(n-1)/500+1,:enc,:hmac,:mask,:version,'sent',:created_at "
            "FROM generate_series(1,:size) n"
        ), {
            "enc": filler.phone_enc, "hmac": filler.phone_hmac, "mask": filler.phone_mask,
            "version": filler.key_version, "created_at": CREATED_AT, "size": size,
        })
        for index, status in enumerate((2, 0, 3), start=1):
            phone = crypto.protect_phone(f"139{index:08d}")
            await connection.execute(text(
                "UPDATE sms_message SET phone_enc=:enc,phone_hmac=:hmac,phone_mask=:mask "
                "WHERE id=:id AND created_at=:created_at"
            ), {
                "enc": phone.phone_enc, "hmac": phone.phone_hmac, "mask": phone.phone_mask,
                "id": index, "created_at": CREATED_AT,
            })
            reports.append(ProtectedReport(
                event_key=uuid4().hex + uuid4().hex,
                vendor_task_id="b" * 64,
                custom_id="c" * 64,
                match_custom_id="active-count-1",
                phone_enc=phone.phone_enc,
                phone_hmac=phone.phone_hmac,
                phone_mask=phone.phone_mask,
                key_version=phone.key_version,
                report_status=status,
                message_status={2: "failed", 0: "unknown", 3: "other"}[status],
                report_desc="synthetic",
                report_time=CREATED_AT,
                phone_hmacs=(phone.phone_hmac,),
            ))
        await connection.execute(text("ANALYZE sms_message"))
    return reports


async def assert_facts(engine: AsyncEngine, expected_status: str) -> None:
    async with engine.connect() as connection:
        actual = (await connection.execute(text(
            "SELECT delivered,failed,unknown_cnt,active_message_count,status "
            "FROM sms_batch WHERE id=1"
        ))).mappings().one()
        facts = (await connection.execute(text(
            "SELECT count(*) FILTER (WHERE status='delivered') delivered,"
            "count(*) FILTER (WHERE status='failed') failed,"
            "count(*) FILTER (WHERE status='unknown') unknown_cnt,"
            "count(*) FILTER (WHERE status IN ('pending','sent')) active_message_count "
            "FROM sms_message WHERE batch_id=1"
        ))).mappings().one()
    assert {key: actual[key] for key in facts} == dict(facts)
    assert actual["status"] == expected_status


def relation_names(plan: dict[str, Any]) -> set[str]:
    names = {plan["Relation Name"]} if "Relation Name" in plan else set()
    for child in plan.get("Plans", []):
        names.update(relation_names(child))
    return names


@pytest.mark.parametrize("size", [1_000, 20_000])
async def test_historical_count_initializes_once_and_corrections_never_probe_messages(
    active_env: ActiveCountEnvironment, size: int,
) -> None:
    """旧 snapshot 保留全部 sent 版本，后续成本仍不随批次消息数反复增长。"""

    engine, repository, calls = active_env.engine, active_env.repository, active_env.calls
    reports = await seed_history(engine, size)
    async with engine.connect() as old_reader:
        await old_reader.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        assert await old_reader.scalar(text(
            "SELECT count(*) FROM sms_message WHERE status='sent'"
        )) == size
        async with engine.begin() as connection:
            await connection.execute(text(
                "UPDATE sms_message SET status='delivered' WHERE batch_id=1 AND id>3"
            ))
            await connection.execute(text("ANALYZE sms_message"))

        result = await repository.apply_report(1, reports[0])
        assert result is not None and result.changed
        await assert_facts(engine, "sending")
        assert len(calls) == 1
        assert len(calls[0].message_statements) == 1, "NULL 历史只全聚合一次，不叠当前差值"

        for report, expected_status in zip(reports[1:], ("sending", "completed"), strict=True):
            result = await repository.apply_report(1, report)
            assert result is not None and result.changed
            await assert_facts(engine, expected_status)
            assert not calls[-1].message_statements

        # 从 failed/unknown/other 分别更正为 delivered；随后同状态新事件保留修订身份。
        for report in reports:
            # chunk/调度等元数据更新不能使已初始化的活动计数重复退回全量扫描。
            async with engine.begin() as connection:
                await connection.execute(text(
                    "UPDATE sms_batch SET updated_at=clock_timestamp() WHERE id=1"
                ))
            corrected = replace(
                report, event_key=uuid4().hex + uuid4().hex,
                report_status=1, message_status="delivered",
                report_time=CREATED_AT + timedelta(days=1),
            )
            result = await repository.apply_report(1, corrected)
            assert result is not None and result.changed
            await assert_facts(engine, "completed")
            assert not calls[-1].message_statements
            revised = replace(
                corrected, event_key=uuid4().hex + uuid4().hex,
                report_time=CREATED_AT + timedelta(days=2),
            )
            result = await repository.apply_report(1, revised)
            assert result is not None and result.changed
            await assert_facts(engine, "completed")
            count_before = len(calls)
            repeated = await repository.apply_report(1, revised)
            assert repeated is not None and not repeated.changed
            stale = replace(report, event_key=uuid4().hex + uuid4().hex)
            rejected = await repository.apply_report(1, stale)
            assert rejected is not None and not rejected.changed
            assert len(calls) == count_before

        assert len(calls) == 9
        assert sum(len(call.message_statements) for call in calls) == 1
        # 保证旧 snapshot 在全部后续回执中仍有效，未通过提前清理旧版本使测试变绿。
        assert await old_reader.scalar(text(
            "SELECT count(*) FROM sms_message WHERE status='sent'"
        )) == size
        async with engine.connect() as connection:
            for call in calls[1:]:
                updates = [item for item in call.statements if "UPDATE sms_batch" in item[0]]
                assert len(updates) == 1
                statement, params = updates[0]
                # EXPLAIN 不执行第二遍差值写；真实计划必须仅定位 batch，无消息子计划。
                plan = (await connection.execute(
                    text("EXPLAIN (FORMAT JSON) " + statement), params,
                )).scalar_one()[0]["Plan"]
                assert "sms_message" not in relation_names(plan)
        await old_reader.rollback()


async def test_legacy_writer_invalidates_count_once_before_new_deltas_resume(
    active_env: ActiveCountEnvironment,
) -> None:
    """旧版 writer 未更新 token 即失效；相同值/时间亦不能留下旧缓存。"""

    engine, repository, calls = active_env.engine, active_env.repository, active_env.calls
    reports = await seed_history(engine, 1_000)
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE sms_message SET status='delivered' WHERE batch_id=1 AND id>3"
        ))
    first = await repository.apply_report(1, reports[0])
    assert first is not None and first.changed
    await assert_facts(engine, "sending")
    assert len(calls[0].message_statements) == 1
    async with engine.begin() as connection:
        initial_token = await connection.scalar(text(
            "SELECT active_message_count_token FROM sms_batch WHERE id=1"
        ))
        assert initial_token is not None
        await connection.execute(text(
            "UPDATE sms_batch SET updated_at=clock_timestamp() WHERE id=1"
        ))
        assert await connection.scalar(text(
            "SELECT active_message_count_token FROM sms_batch WHERE id=1"
        )) == initial_token
        # 模拟旧版本的正常 sent→delivered 写入及批次计数更新；它不认识新缓存列。
        await connection.execute(text(
            "UPDATE sms_message SET status='delivered' WHERE id=2 AND created_at=:created_at"
        ), {"created_at": CREATED_AT})
        await connection.execute(text(
            "UPDATE sms_batch SET delivered=delivered+1,updated_at=updated_at WHERE id=1"
        ))
        assert await connection.scalar(text(
            "SELECT active_message_count IS NULL AND active_message_count_token IS NULL "
            "FROM sms_batch WHERE id=1"
        ))
    last_active = await repository.apply_report(1, reports[2])
    assert last_active is not None and last_active.changed
    await assert_facts(engine, "completed")
    assert len(calls) == 2
    assert len(calls[1].message_statements) == 1, "旧 writer 后仅首次回执重建事实计数"
    corrected = replace(
        reports[0], event_key=uuid4().hex + uuid4().hex,
        report_status=1, message_status="delivered",
        report_time=CREATED_AT + timedelta(days=1),
    )
    result = await repository.apply_report(1, corrected)
    assert result is not None and result.changed
    await assert_facts(engine, "completed")
    assert len(calls) == 3 and not calls[-1].message_statements
    async with engine.begin() as connection:
        assert await connection.scalar(text(
            "SELECT active_message_count_token FROM sms_batch WHERE id=1"
        )) != initial_token
        # 旧 writer 即使重写相同计数和相同 timestamp，也必须触发失效。
        await connection.execute(text(
            "UPDATE sms_batch SET delivered=delivered,failed=failed,unknown_cnt=unknown_cnt,"
            "updated_at=updated_at WHERE id=1"
        ))
        assert await connection.scalar(text(
            "SELECT active_message_count IS NULL AND active_message_count_token IS NULL "
            "FROM sms_batch WHERE id=1"
        ))
    revised = replace(
        corrected, event_key=uuid4().hex + uuid4().hex,
        report_time=CREATED_AT + timedelta(days=2),
    )
    result = await repository.apply_report(1, revised)
    assert result is not None and result.changed
    await assert_facts(engine, "completed")
    assert len(calls) == 4 and len(calls[-1].message_statements) == 1


async def test_concurrent_first_reports_from_different_chunks_initialize_once(
    active_env: ActiveCountEnvironment,
) -> None:
    """两个 chunk 并发首次回执由 batch 锁串行化，NULL 初始化和活动差值不丢失。"""

    engine, repository, calls = active_env.engine, active_env.repository, active_env.calls
    reports = await seed_history(engine, 1_000)
    phone = _crypto().protect_phone("13900000501")
    async with engine.begin() as connection:
        await connection.execute(text(
            "UPDATE sms_message SET status='delivered' WHERE id NOT IN (1,501)"
        ))
        await connection.execute(text(
            "UPDATE sms_message SET phone_enc=:enc,phone_hmac=:hmac,phone_mask=:mask "
            "WHERE id=501 AND created_at=:created_at"
        ), {
            "enc": phone.phone_enc, "hmac": phone.phone_hmac, "mask": phone.phone_mask,
            "created_at": CREATED_AT,
        })
    other_chunk = replace(
        reports[0], event_key=uuid4().hex + uuid4().hex, match_custom_id="active-count-2",
        phone_enc=phone.phone_enc, phone_hmac=phone.phone_hmac, phone_mask=phone.phone_mask,
        phone_hmacs=(phone.phone_hmac,), report_status=1, message_status="delivered",
    )
    results = await asyncio.gather(
        repository.apply_report(1, reports[0]), repository.apply_report(1, other_chunk),
    )
    assert all(result is not None and result.changed for result in results)
    await assert_facts(engine, "completed")
    assert len(calls) == 2
    assert sum(len(call.message_statements) for call in calls) == 1


async def test_migration_upgrade_and_downgrade_preserve_facts_and_trigger_boundary(
    active_env: ActiveCountEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在合成表上执行实际 0111 双向 DDL，验证 nullable 懒初始化与失效触发器。"""

    path = ROOT / "backend/migrations/versions/0111_report_batch_active_count.py"
    spec = importlib.util.spec_from_file_location("active_count_revision_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upgrade: list[Any] = []
    downgrade: list[Any] = []
    with monkeypatch.context() as capture:
        capture.setattr(module.op, "execute", upgrade.append)
        module.upgrade()
        capture.setattr(module.op, "execute", downgrade.append)
        module.downgrade()

    async with active_env.engine.begin() as connection:
        for statement in downgrade:
            await connection.execute(text(str(statement)))
        assert await connection.scalar(text(
            "SELECT to_regprocedure('invalidate_legacy_batch_active_count()') IS NULL"
        ))
        await connection.execute(text(
            "INSERT INTO sms_batch(id,batch_no,status,delivered,failed,unknown_cnt) "
            "VALUES(1,'migration-history','sending',7,2,1)"
        ))
        for statement in upgrade:
            await connection.execute(text(str(statement)))
        row = (await connection.execute(text(
            "SELECT delivered,failed,unknown_cnt,active_message_count,active_message_count_token "
            "FROM sms_batch WHERE id=1"
        ))).one()
        assert tuple(row) == (7, 2, 1, None, None), "升级不回填或改变历史事实"
        await connection.execute(text(
            "UPDATE sms_batch SET active_message_count=2,"
            "active_message_count_token=gen_random_uuid() WHERE id=1"
        ))
        await connection.execute(text("UPDATE sms_batch SET delivered=8 WHERE id=1"))
        assert await connection.scalar(text(
            "SELECT active_message_count IS NULL AND active_message_count_token IS NULL "
            "FROM sms_batch WHERE id=1"
        )), "实际迁移创建的旧 writer 触发器必须有效"
        for statement in downgrade:
            await connection.execute(text(str(statement)))
        assert tuple((await connection.execute(text(
            "SELECT delivered,failed,unknown_cnt FROM sms_batch WHERE id=1"
        ))).one()) == (8, 2, 1)
        assert await connection.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() "
            "AND table_name='sms_batch' AND column_name LIKE 'active_message_count%'"
        )) == 0
        for statement in upgrade:
            await connection.execute(text(str(statement)))
        assert await connection.scalar(text(
            "SELECT active_message_count IS NULL AND active_message_count_token IS NULL "
            "FROM sms_batch WHERE id=1"
        ))
