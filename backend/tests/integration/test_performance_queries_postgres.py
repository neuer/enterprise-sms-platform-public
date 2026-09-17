"""隔离 PostgreSQL 中执行实际统计/列表 SQL；临时表沿用生产列、约束与索引。"""

from __future__ import annotations

import base64
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.jobtrack import JobSpec
from app.services.batch_query import BatchAccessScope, BatchQueryService
from app.services.crypto import CryptoService, EncryptionContext
from app.services.current_alerts_repository import SqlCurrentAlertRepository
from app.services.dashboard_repository import SqlDashboardRepository
from app.services.stats import SHANGHAI, success_rate, success_rate_sql
from app.services.stats_repository import AGGREGATE_SQL, SqlStatsRepository

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

DAY = date(2026, 7, 12)
START = datetime(2026, 7, 11, 16, tzinfo=UTC)


class BoundEngine:
    """把真实仓储绑定到单个回滚事务，并记录其真实 SQL 调用。"""

    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[BoundEngine]:
        yield self

    begin = connect

    async def execute(self, statement: Any, parameters: Any = None) -> Any:
        self.calls.append((str(statement), parameters))
        return await self.connection.execute(statement, parameters)

    async def dispose(self) -> None:
        return None


@pytest.fixture
async def facts() -> AsyncIterator[
    tuple[BoundEngine, CryptoService, list[dict[str, Any]], list[dict[str, Any]]]
]:
    assert os.environ.get("ENVIRONMENT") == "test"
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    assert database_url.host in {"127.0.0.1", "localhost"}, "requires loopback PostgreSQL"
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in ("app", "sms_batch", "sms_message", "stat_daily", "stat_dirty_date"):
                    await connection.execute(
                        text(
                            f"CREATE TEMP TABLE {table} "
                            f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                            "INCLUDING INDEXES) ON COMMIT DROP"
                        )
                    )
                key = base64.b64encode(b"p" * 32).decode()
                crypto = CryptoService.from_secret_values(key, key)
                batches: list[dict[str, Any]] = []
                for batch_id in range(1, 13):
                    batch_no = f"performance-{batch_id}"
                    values = {
                        "id": batch_id,
                        "batch_no": batch_no,
                        "app_id": None if batch_id % 3 == 0 else batch_id % 2 + 1,
                        "dept": "A" if batch_id % 2 else "B",
                        "category": ("notice", "verify", "market")[batch_id % 3],
                        "segments": batch_id % 3 + 1,
                        "channel": "api" if batch_id % 2 else "web",
                        "status": ("sending", "completed", "queued")[batch_id % 3],
                        "created_at": START + timedelta(minutes=batch_id),
                        "is_test": batch_id % 2 == 0,
                        "content": crypto.encrypt_bound_packed_text(
                            "合成测试内容",
                            EncryptionContext(
                                domain="sms-display-content",
                                table="sms_batch",
                                column="display_content_enc",
                                object_id=batch_no,
                            ),
                        ),
                    }
                    batches.append(values)
                await connection.execute(
                    text(
                        "INSERT INTO sms_batch(id,batch_no,app_id,dept,category,segments,channel,"
                        "status,created_at,is_test,display_content_enc,send_content_enc) VALUES "
                        "(:id,:batch_no,:app_id,:dept,:category,:segments,:channel,:status,"
                        ":created_at,:is_test,:content,:content)"
                    ),
                    batches,
                )
                messages: list[dict[str, Any]] = []
                for message_id in range(1, 1201):
                    messages.append(
                        {
                            "id": message_id,
                            "batch_id": (message_id - 1) % 12 + 1,
                            "status": (
                                "pending",
                                "sent",
                                "delivered",
                                "failed",
                                "unknown",
                                "other",
                            )[(message_id // 12) % 6],
                            "created_at": START + timedelta(seconds=message_id),
                        }
                    )
                # 两侧自然日边界：end 恰好不属于本日；同批跨日亦不能多算。
                messages.extend(
                    [
                        {
                            "id": 1201,
                            "batch_id": 1,
                            "status": "failed",
                            "created_at": START - timedelta(microseconds=1),
                        },
                        {
                            "id": 1202,
                            "batch_id": 1,
                            "status": "delivered",
                            "created_at": START + timedelta(days=1),
                        },
                    ]
                )
                protected = crypto.protect_phone("13800000000", table="sms_message")
                await connection.execute(
                    text(
                        "INSERT INTO sms_message(id,batch_id,status,created_at,"
                        "phone_enc,phone_hmac,"
                        "phone_mask,key_version) VALUES (:id,:batch_id,:status,:created_at,"
                        ":phone_enc,:phone_hmac,:phone_mask,:key_version)"
                    ),
                    [
                        dict(
                            row,
                            phone_enc=protected.phone_enc,
                            phone_hmac=protected.phone_hmac,
                            phone_mask=protected.phone_mask,
                            key_version=protected.key_version,
                        )
                        for row in messages
                    ],
                )
                await connection.execute(text("ANALYZE sms_batch"))
                await connection.execute(text("ANALYZE sms_message"))
                yield BoundEngine(connection), crypto, batches, messages
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _expected_stats(
    batches: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> dict[tuple[str, str, str], tuple[int, ...]]:
    by_id = {row["id"]: row for row in batches}
    totals: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for message in messages:
        if message["created_at"].astimezone(SHANGHAI).date() != DAY:
            continue
        batch = by_id[message["batch_id"]]
        dimensions = [("all", ""), ("dept", batch["dept"])]
        if batch["app_id"] is not None:
            dimensions.append(("app", str(batch["app_id"])))
        for dimension in dimensions:
            for category in (batch["category"], "all"):
                bucket = totals[(*dimension, category)]
                bucket[0] += 1
                bucket[1] += batch["segments"]
                bucket[2] += message["status"] == "delivered"
                bucket[3] += message["status"] == "failed"
                bucket[4] += message["status"] in {"unknown", "other"}
    return {key: tuple(value) for key, value in totals.items()}


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node,
        *(descendant for child in node.get("Plans", []) for descendant in _plan_nodes(child)),
    ]


async def test_stats_actual_sql_matches_facts_and_expands_only_batch_totals(facts: Any) -> None:
    bound, _crypto, batches, messages = facts
    repository = SqlStatsRepository()
    repository._engine = lambda: bound  # type: ignore[method-assign]
    for correct_status in (None, "delivered", "failed"):
        if correct_status is not None:
            messages[0]["status"] = correct_status
            await bound.connection.execute(
                text("UPDATE sms_message SET status=:status WHERE id=1"), {"status": correct_status}
            )
        await repository.aggregate_day(DAY)
        rows = (await bound.connection.execute(text("SELECT * FROM stat_daily"))).mappings()
        actual = {
            (row["dim_type"], row["dim_value"], row["category"]): tuple(
                row[column]
                for column in ("total", "total_segments", "delivered", "failed", "unknown_cnt")
            )
            for row in rows
        }
        assert actual == _expected_stats(batches, messages)
    await repository.aggregate_day(DAY + timedelta(days=3))
    assert (
        await bound.connection.scalar(
            text("SELECT count(*) FROM stat_daily WHERE stat_date=:day"),
            {"day": DAY + timedelta(days=3)},
        )
        == 0
    )
    await bound.connection.execute(text("DELETE FROM stat_daily"))
    plan = (
        await bound.connection.execute(
            text("EXPLAIN (ANALYZE, FORMAT JSON) " + str(AGGREGATE_SQL)),
            {"stat_date": DAY, "start_at": START, "end_at": START + timedelta(days=1)},
        )
    ).scalar_one()[0]["Plan"]
    preaggregates = [
        node
        for node in _plan_nodes(plan)
        if node["Node Type"] == "Aggregate" and "m.batch_id" in node.get("Group Key", [])
    ]
    assert len(preaggregates) == 1
    assert preaggregates[0]["Actual Rows"] == 12
    assert preaggregates[0]["Plans"][0]["Actual Rows"] >= 1200
    for delivered, failed in ((0, 0), (8, 2), (1, 7)):
        actual_rate = await bound.connection.scalar(
            text("SELECT " + success_rate_sql(":delivered", ":failed")),
            {"delivered": delivered, "failed": failed},
        )
        assert actual_rate == success_rate(delivered, failed)


async def test_batch_actual_service_reuses_facets_for_scope_status_and_pagination(
    facts: Any,
) -> None:
    bound, crypto, batches, _messages = facts
    service = BatchQueryService(crypto=crypto)
    service._engine = lambda: bound  # type: ignore[method-assign]
    for scope in (
        BatchAccessScope(all_departments=True),
        BatchAccessScope(dept="A"),
        BatchAccessScope(app_id=1),
    ):
        eligible = [
            row
            for row in batches
            if (scope.dept is None or row["dept"] == scope.dept)
            and (scope.app_id is None or row["app_id"] == scope.app_id)
        ]
        facets = {
            status: sum(row["status"] == status for row in eligible)
            for status in {row["status"] for row in eligible}
        }
        for statuses in (None, [], ["sending"], ["queued", "sending", "queued"], ["absent"]):
            for page in (1, 3, 30):
                bound.calls.clear()
                result = await service.list_batches(
                    scope=scope,
                    category=None,
                    statuses=statuses,
                    channel=None,
                    app_id=None,
                    is_test=None,
                    batch_no=None,
                    start=None,
                    end=None,
                    page=page,
                    size=2,
                )
                selected = sorted(
                    (row for row in eligible if not statuses or row["status"] in statuses),
                    key=lambda row: (row["created_at"], row["id"]),
                    reverse=True,
                )
                assert result["total"] == len(selected)
                assert result["status_counts"] == facets
                assert [item["batch_no"] for item in result["items"]] == [
                    row["batch_no"] for row in selected[(page - 1) * 2 : page * 2]
                ]
                assert len(bound.calls) == 2
                if scope.all_departments and statuses is None:
                    statement, parameters = bound.calls[1]
                    plan = (
                        await bound.connection.execute(
                            text("EXPLAIN (ANALYZE, FORMAT JSON) " + statement), parameters
                        )
                    ).scalar_one()[0]["Plan"]
                    per_batch = [
                        node
                        for node in _plan_nodes(plan)
                        if node["Node Type"] == "Aggregate" and not node.get("Group Key")
                    ]
                    assert per_batch
                    assert all(node["Actual Loops"] <= 2 for node in per_batch)
    bound.calls.clear()
    result = await service.list_batches(
        scope=BatchAccessScope(all_departments=True),
        category="notice",
        statuses=None,
        channel="web",
        app_id=None,
        is_test=True,
        batch_no="performance-",
        start=START,
        end=START + timedelta(minutes=10),
        page=1,
        size=2,
    )
    assert result["total"] == 1
    assert result["status_counts"] == {"sending": 1}
    assert result["items"][0]["batch_no"] == "performance-6"


async def test_shared_current_facts_match_legacy_dashboard_sql_without_duplicate_reads(
    facts: Any,
) -> None:
    """真实 PostgreSQL 验证合并 count/min 后计数、最新任务、未知余额均不变。"""

    bound, _crypto, _batches, _messages = facts
    for table in (
        "sys_config",
        "job_run",
        "usage_projection_drift",
        "balance_snapshot",
        "sms_chunk",
        "callback_task",
        "outbox_event",
        "raw_vendor_log",
        "alert_log",
        "approval",
        "unmatched_report",
    ):
        await bound.connection.execute(
            text(
                f"CREATE TEMP TABLE {table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                "INCLUDING INDEXES) ON COMMIT DROP"
            )
        )
    now = await bound.connection.scalar(text("SELECT now()"))
    assert isinstance(now, datetime)
    await bound.connection.execute(
        text(
            "INSERT INTO app(id,name,dept,api_key_hash,api_key_prefix,created_by) "
            "VALUES(1,'synthetic','A',:digest,'test0001','synthetic')"
        ),
        {"digest": "a" * 64},
    )
    await bound.connection.execute(
        text(
            "INSERT INTO sms_chunk(id,batch_id,chunk_no,custom_id,"
            "phone_count,status,uncertain_since) "
            "VALUES(:id,1,:chunk_no,:custom,1,'uncertain',:since)"
        ),
        [
            {"id": 1, "chunk_no": 1, "custom": "uncertain-old", "since": now - timedelta(hours=48)},
            {"id": 2, "chunk_no": 2, "custom": "uncertain-new", "since": now - timedelta(hours=1)},
        ],
    )
    await bound.connection.execute(
        text(
            "INSERT INTO callback_task(id,app_id,event,url,callback_secret_enc,"
            "callback_secret_key_version,status,created_at) VALUES "
            "(:id,1,'batch.finished','https://callback.invalid',:ciphertext,1,:status,:created)"
        ),
        [
            {
                "id": 1,
                "ciphertext": b"synthetic-ciphertext",
                "status": "dead",
                "created": now - timedelta(hours=2),
            },
            {"id": 2, "ciphertext": b"synthetic-ciphertext", "status": "done", "created": now},
        ],
    )
    await bound.connection.execute(
        text(
            "INSERT INTO outbox_event(id,dedup_key,event_type,aggregate_type,aggregate_id,"
            "task_name,queue,state,created_at) VALUES(:id,:dedup,'job','job','synthetic',"
            "'app.tasks.outbox.trigger_job',:queue,:state,:created)"
        ),
        [
            {
                "id": uuid4(),
                "dedup": f"synthetic-{index}",
                "queue": queue,
                "state": state,
                "created": now - timedelta(minutes=index),
            }
            for index, (queue, state) in enumerate(
                (
                    ("realtime", "pending"),
                    ("bulk", "processing"),
                    ("bulk", "dead"),
                    ("realtime", "completed"),
                    ("realtime-report", "pending"),
                )
            )
        ],
    )
    await bound.connection.execute(
        text(
            "INSERT INTO job_run(id,job_name,started_at,status) VALUES "
            "(1,'poll_report',:older,'success'),(2,'poll_report',:newer,'running')"
        ),
        {"older": now - timedelta(minutes=1), "newer": now},
    )
    specs = (JobSpec("poll_report", 60), JobSpec("absent_job", 300))
    current_repository = SqlCurrentAlertRepository()
    current_repository._engine = lambda: bound  # type: ignore[method-assign]
    bound.calls.clear()
    current = await current_repository.load_database(specs)
    assert len(bound.calls) == 5
    assert current.balance is None and current.balance_checked_at is None
    assert (current.uncertain, current.uncertain_overdue) == (2, 1)
    assert current.uncertain_since == now - timedelta(hours=48)
    assert current.callback_dead == 1
    assert current.callback_dead_since == now - timedelta(hours=2)
    assert (current.outbox_active, current.outbox_dead) == (3, 1)
    assert (current.realtime_queue, current.bulk_queue) == (1, 1)
    assert current.outbox_oldest_active_at == now - timedelta(minutes=4)

    class IdleRedis:
        async def hget(self, _key: str, _field: str) -> None:
            return None

    dashboard = SqlDashboardRepository()
    dashboard._engine = lambda: bound  # type: ignore[method-assign]
    dashboard.redis = IdleRedis()  # type: ignore[attr-defined]
    today = now.astimezone(SHANGHAI).date()
    bound.calls.clear()
    legacy = await dashboard.load(None, today, include_operations=True, job_specs=specs)
    assert len(bound.calls) == 10
    bound.calls.clear()
    shared = await dashboard.load(
        None,
        today,
        include_operations=True,
        job_specs=specs,
        current_facts=current,
        include_legacy_alerts=False,
    )
    assert len(bound.calls) == 5
    assert shared == legacy
