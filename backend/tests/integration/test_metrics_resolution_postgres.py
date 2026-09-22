"""人工处置指标按 PostgreSQL 完成事实计数，使用真实只读运行角色。"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_manual_resolved_counts_only_closed_applied_facts_and_not_replays() -> None:
    """用真实 metrics 角色计数，未完成处置和重复执行均不得额外计入。"""
    from app.services.metrics_repository import SqlMetricsRepository

    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:

                @asynccontextmanager
                async def metrics_connection():
                    await connection.execute(text("SET LOCAL ROLE sms_metrics"))
                    try:
                        yield connection
                    finally:
                        await connection.execute(text("RESET ROLE"))

                repository = SqlMetricsRepository()
                repository._engine = lambda: SimpleNamespace(connect=metrics_connection)

                async def count() -> int:
                    facts = await repository.load()
                    return dict(facts.uncertain_lifecycle)["manual_resolved"]

                baseline = await count()
                proposer = await connection.scalar(
                    text("INSERT INTO user_account DEFAULT VALUES RETURNING id")
                )
                confirmer = await connection.scalar(
                    text("INSERT INTO user_account DEFAULT VALUES RETURNING id")
                )
                batch = await connection.scalar(
                    text(
                        "INSERT INTO sms_batch(batch_no,channel,dept,content,status,"
                        "display_content_enc,send_content_enc) VALUES(:number,'web','synthetic',"
                        "'[encrypted]','completed_unknown',decode('aa','hex'),decode('bb','hex')) "
                        "RETURNING id"
                    ),
                    {"number": uuid4().hex},
                )
                pending_id = None
                cases = [
                    ("proposed", False),
                    ("effect_pending", False),
                    ("effect_applied", True),
                    ("closed", False),
                    ("closed", True),
                ]
                for ordinal, (state, applied) in enumerate(cases, 1):
                    chunk = await connection.scalar(
                        text(
                            "INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status) "
                            "VALUES(:batch,:ordinal,:custom,1,'unknown_terminal') RETURNING id"
                        ),
                        {"batch": batch, "ordinal": ordinal, "custom": uuid4().hex},
                    )
                    resolution = await connection.scalar(
                        text(
                            "INSERT INTO sms_uncertain_resolution(chunk_id,batch_id,action,state,"
                            "proposer_account_id,confirmer_account_id,"
                            "confirmed_at,effect_applied_at) "
                            "VALUES(:chunk,:batch,'keep_unknown',:state,:proposer,:confirmer,"
                            "CASE WHEN :confirmed THEN now() END,"
                            "CASE WHEN :applied THEN now() END) RETURNING id"
                        ),
                        {
                            "chunk": chunk,
                            "batch": batch,
                            "state": state,
                            "proposer": proposer,
                            "confirmer": confirmer if state != "proposed" else None,
                            "confirmed": state != "proposed",
                            "applied": applied,
                        },
                    )
                    if state == "effect_pending":
                        pending_id = resolution
                assert await count() == baseline + 1
                for _ in range(2):
                    await connection.execute(
                        text(
                            "UPDATE sms_uncertain_resolution SET state='closed',"
                            "effect_applied_at=now() "
                            "WHERE id=:id"
                        ),
                        {"id": pending_id},
                    )
                    assert await count() == baseline + 2
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
