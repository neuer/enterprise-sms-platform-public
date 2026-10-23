"""真实 PostgreSQL 上验证 test_scope 迁移、分层聚合与筛选未就绪。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.reporting import ReportFilterNotReady, ReportingService
from app.services.reporting_repository import SqlReportingRepository
from app.services.stats import success_rate
from app.services.stats_repository import SqlStatsRepository

pytestmark = pytest.mark.skipif(
    "SMS_ISOLATED_TEST_DATABASE" not in os.environ or "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires official isolated PostgreSQL",
)

_OLD_STAT_DAILY = """
CREATE TABLE stat_daily (
  stat_date date NOT NULL,
  dim_type varchar(8) NOT NULL,
  dim_value varchar(128) NOT NULL DEFAULT '',
  category varchar(8) NOT NULL DEFAULT 'all',
  total integer NOT NULL DEFAULT 0,
  total_segments integer NOT NULL DEFAULT 0,
  delivered integer NOT NULL DEFAULT 0,
  failed integer NOT NULL DEFAULT 0,
  unknown_cnt integer NOT NULL DEFAULT 0,
  PRIMARY KEY (stat_date, dim_type, dim_value, category)
)
"""

DAY = date(2026, 7, 12)
START = datetime(2026, 7, 11, 16, tzinfo=UTC)
MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/versions/0130_stat_daily_test_scope.py"
)


def _engine():
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.database != "postgres"
    return create_async_engine(url)


def _upgrade_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("stat_scope_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.UPGRADE_STATEMENTS)


def _repositories() -> tuple[SqlStatsRepository, ReportingService]:
    stats = SqlStatsRepository()
    reporting = SqlReportingRepository()
    stats._engine = _engine  # type: ignore[method-assign]
    reporting._engine = _engine  # type: ignore[method-assign]
    return stats, ReportingService(reporting)


async def _insert_batch(
    connection,
    *,
    batch_no: str,
    dept: str,
    is_test: bool,
    segments: int,
) -> int:
    return int(
        await connection.scalar(
            text(
                """
                INSERT INTO sms_batch(
                  batch_no,channel,dept,category,is_test,segments,
                  display_content_enc,send_content_enc
                ) VALUES (
                  :batch_no,'web',:dept,'notice',:is_test,:segments,
                  :payload,:payload
                ) RETURNING id
                """
            ),
            {
                "batch_no": batch_no,
                "dept": dept,
                "is_test": is_test,
                "segments": segments,
                "payload": b"\x01",
            },
        )
    )


async def _insert_messages(connection, batch_id: int, statuses: list[str], at: datetime) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO sms_message(
              batch_id,status,created_at,phone_enc,phone_hmac,phone_mask,key_version
            ) VALUES (
              :batch_id,:status,:created_at,:phone_enc,:phone_hmac,:phone_mask,1
            )
            """
        ),
        [
            {
                "batch_id": batch_id,
                "status": status,
                "created_at": at + timedelta(seconds=index + 1),
                "phone_enc": b"\x02",
                "phone_hmac": f"{batch_id:016x}{index:048x}",
                "phone_mask": "000****0000",
            }
            for index, status in enumerate(statuses)
        ],
    )


async def test_upgrade_keeps_mixed_rows_as_all_and_can_roll_forward() -> None:
    """旧主键上的混合行升级后仍是 all；中断回滚后再次执行得到同一结果。"""

    engine = _engine()
    upgrade_statements = _upgrade_statements()
    try:
        async with engine.connect() as connection:
            public = (
                await connection.execute(
                    text(
                        """
                        SELECT column_default,
                          has_table_privilege('sms_accept','public.stat_scope_day','SELECT'),
                          has_table_privilege('sms_accept','public.stat_scope_day','INSERT'),
                          has_table_privilege('sms_send','public.stat_scope_day','SELECT'),
                          has_table_privilege('sms_send','public.stat_scope_day','INSERT'),
                          has_table_privilege('sms_send','public.stat_scope_day','UPDATE'),
                          has_table_privilege('sms_send','public.stat_scope_day','DELETE'),
                          (
                            SELECT pg_get_constraintdef(oid) FROM pg_constraint
                            WHERE conrelid='public.stat_daily'::regclass AND contype='p'
                          )
                        FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='stat_daily'
                          AND column_name='test_scope'
                        """
                    )
                )
            ).one()
        assert "all" in str(public[0]) and "normal" not in str(public[0])
        assert tuple(public[1:7]) == (True, False, True, True, True, False)
        assert "test_scope" in str(public[7])

        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("SAVEPOINT before_upgrade"))
                await connection.execute(text("CREATE SCHEMA stat_scope_mig"))
                await connection.execute(text("SET LOCAL search_path TO stat_scope_mig"))
                await connection.execute(text(_OLD_STAT_DAILY))
                await connection.execute(
                    text(
                        """
                        INSERT INTO stat_daily(
                          stat_date,dim_type,dim_value,category,total
                        ) VALUES ('2020-01-03','dept','合成部门','all',11)
                        """
                    )
                )
                for statement in upgrade_statements:
                    await connection.execute(text(statement))
                await connection.execute(text("ROLLBACK TO SAVEPOINT before_upgrade"))
                await connection.execute(text("CREATE SCHEMA stat_scope_mig"))
                await connection.execute(text("SET LOCAL search_path TO stat_scope_mig"))
                await connection.execute(text(_OLD_STAT_DAILY))
                await connection.execute(
                    text(
                        """
                        INSERT INTO stat_daily(
                          stat_date,dim_type,dim_value,category,total
                        ) VALUES ('2020-01-03','dept','合成部门','all',11)
                        """
                    )
                )
                for statement in upgrade_statements:
                    await connection.execute(text(statement))
                for statement in upgrade_statements:
                    await connection.execute(text(statement))
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT snapshot.test_scope, snapshot.total, columns.column_default
                            FROM stat_daily snapshot
                            JOIN information_schema.columns AS columns
                              ON columns.table_schema=current_schema()
                             AND columns.table_name='stat_daily'
                             AND columns.column_name='test_scope'
                            """
                        )
                    )
                ).one()
                assert (row[0], row[1]) == ("all", 11)
                assert "all" in str(row[2]) and "normal" not in str(row[2])
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM stat_daily WHERE test_scope<>'all'")
                    )
                    == 0
                )
                definition = await connection.scalar(
                    text(
                        """
                        SELECT pg_get_constraintdef(oid) FROM pg_constraint
                        WHERE conrelid='stat_daily'::regclass AND contype='p'
                        """
                    )
                )
                assert "test_scope" in str(definition)
                await connection.execute(
                    text(
                        """
                        INSERT INTO stat_scope_day(
                          stat_date,aggregation_version,completed_at
                        ) VALUES ('2020-01-01',1,clock_timestamp())
                        """
                    )
                )
                assert await connection.scalar(text("SELECT count(*) FROM stat_scope_day")) == 1
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_layered_aggregate_filters_boundaries_and_concurrent_replace() -> None:
    """5 条测试加 6 条普通按各自口径返回，空日完成，未补算的筛选明确未就绪。"""

    engine = _engine()
    stats, service = _repositories()
    batch_ids: list[int] = []
    try:
        async with engine.begin() as connection:
            test_batch = await _insert_batch(
                connection,
                batch_no="83400000000000000000000000000001",
                dept="合成部门",
                is_test=True,
                segments=2,
            )
            normal_batch = await _insert_batch(
                connection,
                batch_no="83400000000000000000000000000002",
                dept="合成部门",
                is_test=False,
                segments=1,
            )
            other_batch = await _insert_batch(
                connection,
                batch_no="83400000000000000000000000000003",
                dept="其他部门",
                is_test=False,
                segments=3,
            )
            batch_ids.extend((test_batch, normal_batch, other_batch))
            await _insert_messages(
                connection,
                test_batch,
                ["delivered", "delivered", "delivered", "failed", "failed"],
                START,
            )
            await _insert_messages(
                connection,
                normal_batch,
                ["delivered", "delivered", "delivered", "delivered", "failed", "unknown"],
                START,
            )
            await _insert_messages(
                connection,
                other_batch,
                ["delivered", "delivered", "delivered", "delivered"],
                START,
            )
            await _insert_messages(
                connection,
                test_batch,
                ["failed"],
                START - timedelta(seconds=2),
            )
            await _insert_messages(
                connection,
                normal_batch,
                ["delivered"],
                START + timedelta(days=1),
            )
            app_id = int(
                await connection.scalar(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,allowed_categories,
                          daily_quota,created_by
                        ) VALUES (
                          '合成应用','合成部门',
                          'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                          '834scope','notice',1,'synthetic'
                        ) RETURNING id
                        """
                    )
                )
            )
            await connection.execute(
                text("UPDATE sms_batch SET app_id=:app_id WHERE dept='合成部门'"),
                {"app_id": app_id},
            )

        async def rebuild() -> None:
            assert await stats.aggregate_day(DAY) > 0

        await asyncio.gather(rebuild(), rebuild())
        await stats.aggregate_day(DAY)

        async def report(**kwargs):
            return await service.get(
                granularity=kwargs.get("granularity", "day"),
                group_by=kwargs.get("group_by", "dept"),
                category=kwargs.get("category", "notice"),
                start=kwargs.get("start", DAY),
                end=kwargs.get("end", DAY),
                role=kwargs.get("role", "viewer"),
                dept=kwargs.get("dept", "合成部门"),
                page=kwargs.get("page", 1),
                size=kwargs.get("size", 20),
                is_test=kwargs.get("is_test"),
            )

        omitted, tested, normal = await asyncio.gather(
            report(),
            report(is_test=True),
            report(is_test=False),
        )
        assert (omitted.summary.total, tested.summary.total, normal.summary.total) == (11, 5, 6)
        assert omitted.summary.total == tested.summary.total + normal.summary.total
        assert (
            omitted.summary.total_segments
            == tested.summary.total_segments + normal.summary.total_segments
            == 16
        )
        assert (tested.summary.total_segments, normal.summary.total_segments) == (10, 6)
        assert (
            omitted.summary.delivered,
            omitted.summary.failed,
            omitted.summary.unknown,
        ) == (7, 3, 1)
        assert tested.summary.delivered + normal.summary.delivered == omitted.summary.delivered
        assert tested.summary.failed + normal.summary.failed == omitted.summary.failed
        assert tested.summary.unknown + normal.summary.unknown == omitted.summary.unknown
        assert tested.summary.success_rate == success_rate(3, 2)
        assert normal.summary.success_rate == success_rate(4, 1)
        assert omitted.summary.success_rate == success_rate(7, 3)
        for grain in ("day", "week", "month"):
            for group in ("dept", "app"):
                scoped = await report(granularity=grain, group_by=group)
                assert scoped.summary.total == 11
                assert scoped.dim_summary[0].is_other is False
        assert (await report(category="market")).summary.total == 0
        assert (await report(category="all")).summary.total == 11
        app_all = await report(group_by="app")
        assert app_all.summary.total == 11
        assert app_all.dim_summary[0].dim_label == "合成应用"
        hidden = await report(role="admin", dept="平台部")
        assert hidden.summary.total == 15
        viewer_labels = {item.dim_label for item in omitted.dim_summary}
        assert viewer_labels == {"合成部门"}
        assert omitted.dimension_total == 1
        await stats.aggregate_day(DAY + timedelta(days=1))
        paged = await report(size=1, page=1, end=DAY + timedelta(days=1))
        assert paged.total == 2 and len(paged.items) == 1
        assert paged.summary.total == 12
        assert paged.items[0].total != paged.summary.total

        empty = date(2026, 7, 20)
        assert await stats.aggregate_day(empty) == 0
        empty_report = await report(start=empty, end=empty, is_test=False)
        assert empty_report.summary.total == 0

        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM stat_scope_day WHERE stat_date=:day"),
                {"day": DAY},
            )
        with pytest.raises(ReportFilterNotReady, match="筛选数据尚未就绪"):
            await report(is_test=True)
        still_all = await report()
        assert still_all.summary.total == 11
        await stats.aggregate_day(DAY)
        restored = await report(is_test=False)
        assert restored.summary.total == 6

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO stat_dirty_date(stat_date) VALUES (:day)"),
                {"day": DAY},
            )
            await connection.execute(
                text(
                    """
                    UPDATE sms_message SET status='failed'
                    WHERE batch_id=:batch_id AND status='delivered'
                      AND created_at>=:start_at AND created_at<:end_at
                    """
                ),
                {
                    "batch_id": normal_batch,
                    "start_at": START,
                    "end_at": START + timedelta(days=1),
                },
            )
        assert DAY in await stats.list_dirty_dates()
        await stats.aggregate_day(DAY)
        assert DAY not in await stats.list_dirty_dates()
        recomputed = await report(is_test=False)
        assert recomputed.summary.delivered == 0
        assert recomputed.summary.failed == 5
        assert recomputed.summary.total == 6
    finally:
        async with engine.begin() as connection:
            if batch_ids:
                await connection.execute(
                    text("DELETE FROM sms_message WHERE batch_id = ANY(:ids)"),
                    {"ids": batch_ids},
                )
                await connection.execute(
                    text("DELETE FROM sms_batch WHERE id = ANY(:ids)"),
                    {"ids": batch_ids},
                )
            await connection.execute(text("DELETE FROM app WHERE api_key_prefix='834scope'"))
            days = [DAY, DAY + timedelta(days=1), date(2026, 7, 20)]
            await connection.execute(
                text("DELETE FROM stat_daily WHERE stat_date = ANY(:days)"),
                {"days": days},
            )
            await connection.execute(
                text("DELETE FROM stat_scope_day WHERE stat_date = ANY(:days)"),
                {"days": days},
            )
            await connection.execute(
                text("DELETE FROM stat_dirty_date WHERE stat_date = ANY(:days)"),
                {"days": days},
            )
        await engine.dispose()
