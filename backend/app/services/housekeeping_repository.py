"""生命周期清理 PostgreSQL 仓储：逐表短事务与有界主键页，不触碰审计。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.housekeeping import (
    CleanupCounts,
    CleanupCursor,
    CleanupPage,
    ExpiredImport,
    LifecyclePolicy,
)
from app.services.idempotency_lifecycle import IDEMPOTENCY_LIVE_SQL
from app.settings import Settings, get_settings

# 固定 cutoff 同时约束对象到期、消费/解析租约，运行期间新到期对象留给下轮。
IMPORT_ELIGIBLE = """
    t.expires_at<=CAST(:cutoff AS timestamptz)
    AND (t.parse_status<>'processing' OR t.parse_lease_expires_at<=CAST(:cutoff AS timestamptz))
    AND (t.state='ready'
      OR (t.state='reserved' AND t.reservation_expires_at<=CAST(:cutoff AS timestamptz))
      OR (t.state='consumed' AND t.payload_purged_at IS NULL))
"""
USAGE_ELIGIBLE = """
    r.usage_date < ((CAST(:cutoff AS timestamptz) AT TIME ZONE 'Asia/Shanghai')::date
                   - CAST(:usage_days AS integer))
    AND r.state IN ('released','committed')
    AND NOT EXISTS (SELECT 1 FROM usage_quota_entry q
                    WHERE q.reservation_id=r.id AND q.expires_at>CAST(:cutoff AS timestamptz))
    AND NOT EXISTS (SELECT 1 FROM usage_frequency_entry f
                    WHERE f.reservation_id=r.id AND f.expires_at>CAST(:cutoff AS timestamptz))
    AND NOT EXISTS (SELECT 1 FROM usage_chunk_allocation a WHERE a.reservation_id=r.id)
"""
SUBJECT_ELIGIBLE = """
    s.created_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:usage_days)
    AND NOT EXISTS (SELECT 1 FROM usage_frequency_entry f WHERE f.subject_id=s.id)
"""


@dataclass(frozen=True)
class _DeletePlan:
    table: str
    keys: tuple[tuple[str, str], ...]
    predicate: str
    count_field: str | None = None
    source: str = ""
    locks: str = "p"


# 名称、标识符及 SQL 全部来自此固定白名单；不接受外部 SQL/表名。
PLANS = {
    "raw": _DeletePlan(
        "raw_vendor_log",
        (("id", "bigint"),),
        """
        p.fetched_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:raw_days)
        AND p.processed = TRUE
        AND (p.processing_lease_expires_at IS NULL
             OR p.processing_lease_expires_at<=CAST(:cutoff AS timestamptz))
        AND (p.system_replay_audit_state IS NULL OR p.system_replay_audit_state<>'pending')
        AND NOT EXISTS (SELECT 1 FROM sms_chunk c WHERE c.status='uncertain'
                        AND c.custom_id=ANY(p.custom_ids))
    """,
        "raw",
    ),
    "unmatched": _DeletePlan(
        "unmatched_report",
        (("id", "bigint"),),
        "p.created_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:unmatched_days)",
        "unmatched",
    ),
    "import_phones": _DeletePlan(
        "import_phone",
        (("id", "bigint"),),
        IMPORT_ELIGIBLE,
        source="JOIN import_task t ON t.id=p.import_task_id",
        locks="t",
    ),
    "idempotency": _DeletePlan(
        "idempotency_record", (("id", "bigint"),),
        "p.expires_at<=CAST(:cutoff AS timestamptz)", "idempotency",
    ),
    "jobs": _DeletePlan(
        "job_run",
        (("id", "bigint"),),
        "p.started_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:job_days) "
        "AND p.status<>'running'",
        "jobs",
    ),
    "callbacks": _DeletePlan(
        "callback_task",
        (("id", "bigint"),),
        "p.status IN ('done','dead') "
        "AND p.created_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:raw_days)",
    ),
    "callback_events": _DeletePlan(
        "callback_report_event",
        (("event_key", "char(64)"),),
        """
        p.created_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:raw_days)
        AND NOT EXISTS (SELECT 1 FROM callback_task t
                        WHERE t.event_keys @> ARRAY[p.event_key]::char(64)[])
    """,
        locks="",
    ),
    "usage_frequency": _DeletePlan(
        "usage_frequency_entry",
        (("reservation_id", "uuid"), ("subject_id", "uuid"), ("window_kind", "varchar")),
        USAGE_ELIGIBLE,
        source="JOIN usage_reservation r ON r.id=p.reservation_id",
        locks="p,r",
    ),
    "usage_quota": _DeletePlan(
        "usage_quota_entry",
        (("reservation_id", "uuid"), ("dimension_kind", "varchar")),
        USAGE_ELIGIBLE,
        source="JOIN usage_reservation r ON r.id=p.reservation_id",
        locks="p,r",
    ),
    "usage": _DeletePlan(
        "usage_reservation",
        (("id", "uuid"),),
        USAGE_ELIGIBLE.replace("r.", "p.")
        + """
        AND NOT EXISTS (SELECT 1 FROM usage_frequency_entry e WHERE e.reservation_id=p.id)
        AND NOT EXISTS (SELECT 1 FROM usage_quota_entry e WHERE e.reservation_id=p.id)
        """,
        "usage",
    ),
    "usage_projection": _DeletePlan(
        "usage_projection",
        (("dimension_key", "varchar"),),
        "p.expires_at < CAST(:cutoff AS timestamptz)-make_interval(days=>:usage_days)",
    ),
    "usage_alias": _DeletePlan(
        "usage_frequency_alias",
        (("subject_id", "uuid"), ("key_version", "smallint")),
        SUBJECT_ELIGIBLE,
        source="JOIN usage_frequency_subject s ON s.id=p.subject_id",
        locks="p,s",
    ),
    "usage_subject": _DeletePlan(
        "usage_frequency_subject",
        (("id", "uuid"),),
        SUBJECT_ELIGIBLE.replace("s.", "p.")
        + """
        AND NOT EXISTS (SELECT 1 FROM usage_frequency_alias a WHERE a.subject_id=p.id)
        """,
    ),
}


class SqlHousekeepingRepository:
    """运行态只使用既有 DML 权限；每页最多 limit 个目标行，父行无大级联。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def policy(self) -> LifecyclePolicy:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("""
                    SELECT key,value FROM sys_config WHERE key IN (
                      'raw_log_retention_days','unmatched_retention_days',
                      'job_history_days','usage_ledger_retention_days')
                """)
                )
                values = {str(row["key"]): int(row["value"]) for row in result.mappings()}
                policy = LifecyclePolicy(
                    values.get("raw_log_retention_days", 90),
                    values.get("unmatched_retention_days", 90),
                    values.get("job_history_days", 30),
                    values.get("usage_ledger_retention_days", 90),
                )
                if (
                    min(policy.raw_days, policy.unmatched_days, policy.job_days, policy.usage_days)
                    < 1
                ):
                    raise ValueError("lifecycle retention values must be positive")
                return policy
        finally:
            await engine.dispose()

    async def expired_imports(
        self,
        *,
        cutoff: datetime,
        after_id: int,
        limit: int,
    ) -> tuple[ExpiredImport, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("invalid housekeeping page size")
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"""
                    SELECT t.id,t.invalid_file,t.source_file FROM import_task t
                    WHERE t.id>:after_id AND {IMPORT_ELIGIBLE}
                      AND NOT EXISTS (SELECT 1 FROM import_phone p WHERE p.import_task_id=t.id)
                    ORDER BY t.id LIMIT :limit
                """),
                    {"cutoff": cutoff, "after_id": after_id, "limit": limit},
                )
                return tuple(
                    ExpiredImport(int(row["id"]), row["invalid_file"], row["source_file"])
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def finish_import(self, import_id: int, *, cutoff: datetime) -> int:
        """仅在子号码全部排空后固化文件清理，不让父 DELETE 触发无界级联。"""

        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(f"""
                    WITH eligible AS MATERIALIZED (
                      SELECT t.id,t.state FROM import_task t
                      WHERE t.id=:import_id AND {IMPORT_ELIGIBLE}
                        AND NOT EXISTS (SELECT 1 FROM import_phone p WHERE p.import_task_id=t.id)
                      FOR UPDATE OF t SKIP LOCKED
                    ), sanitized AS (
                      UPDATE import_task t SET invalid_file=NULL,
                        payload_purged_at=CAST(:cutoff AS timestamptz)
                      FROM eligible e WHERE t.id=e.id AND e.state='consumed' RETURNING t.id
                    ), removed AS (
                      DELETE FROM import_task t USING eligible e
                      WHERE t.id=e.id AND e.state<>'consumed' RETURNING t.id
                    ) SELECT (SELECT count(*) FROM sanitized)+(SELECT count(*) FROM removed)
                """),
                    {"import_id": import_id, "cutoff": cutoff},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    async def cleanup_page(
        self,
        table: str,
        policy: LifecyclePolicy,
        *,
        cutoff: datetime,
        cursor: CleanupCursor | None,
        limit: int,
    ) -> CleanupPage:
        if table not in PLANS or not 1 <= limit <= 1000:
            raise ValueError("invalid housekeeping page")
        plan = PLANS[table]
        if cursor is not None and len(cursor) != len(plan.keys):
            raise ValueError("invalid housekeeping cursor")
        if table == "idempotency":
            return await self._cleanup_idempotency_page(cutoff, cursor, limit)
        params: dict[str, object] = {
            "cutoff": cutoff,
            "raw_days": policy.raw_days,
            "unmatched_days": policy.unmatched_days,
            "job_days": policy.job_days,
            "usage_days": policy.usage_days,
            "limit": limit,
        }
        key_columns = ",".join(f"p.{key}" for key, _ in plan.keys)
        cursor_clause = ""
        if cursor is not None:
            after_columns = ",".join(
                f"CAST(:after_{i} AS {kind})" for i, (_, kind) in enumerate(plan.keys)
            )
            cursor_clause = f"AND ({key_columns}) > ({after_columns})"
            params.update({f"after_{i}": value for i, value in enumerate(cursor)})
        key_join = " AND ".join(f"p.{key}=d.{key}" for key, _ in plan.keys)
        lock_clause = f"FOR UPDATE OF {plan.locks} SKIP LOCKED" if plan.locks else ""
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                # 除行预算外约束锁等待/单语句时间；超时回滚当前页，已提交页不回滚。
                await connection.execute(text("SET LOCAL lock_timeout='1s'"))
                await connection.execute(text("SET LOCAL statement_timeout='5s'"))
                result = await connection.execute(
                    text(f"""
                    WITH due AS MATERIALIZED (
                      SELECT {key_columns} FROM {plan.table} p {plan.source}
                      WHERE {plan.predicate} {cursor_clause}
                      ORDER BY {key_columns} LIMIT :limit
                      {lock_clause}
                    )
                    DELETE FROM {plan.table} p USING due d WHERE {key_join}
                    RETURNING {key_columns}
                """),
                    params,
                )
                keys = [tuple(row[key] for key, _ in plan.keys) for row in result.mappings()]
                count = len(keys)
                return CleanupPage(
                    count,
                    max(keys) if keys else cursor,
                    CleanupCounts(
                        raw=count if plan.count_field == "raw" else 0,
                        unmatched=count if plan.count_field == "unmatched" else 0,
                        imports=count if plan.count_field == "imports" else 0,
                        idempotency=count if plan.count_field == "idempotency" else 0,
                        jobs=count if plan.count_field == "jobs" else 0,
                        usage=count if plan.count_field == "usage" else 0,
                    ),
                )
        finally:
            await engine.dispose()


    async def _cleanup_idempotency_page(
        self, cutoff: datetime, cursor: CleanupCursor | None, limit: int,
    ) -> CleanupPage:
        """锁定有限批次页，重验生命周期并保留完成 Claim 的到期证明。"""
        if cutoff.utcoffset() is None:
            raise ValueError("housekeeping cutoff must include timezone")
        engine = self._engine()
        after_id = cursor[0] if cursor else 0
        if isinstance(after_id, bool) or not isinstance(after_id, int):
            raise ValueError("invalid idempotency cleanup cursor")
        params = {"cutoff": cutoff, "after_id": after_id, "limit": limit}
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='1s'"))
                await connection.execute(text("SET LOCAL statement_timeout='5s'"))
                selected = await connection.execute(text("""
                    SELECT i.id FROM idempotency_record i JOIN sms_batch b ON b.id=i.batch_id
                    WHERE i.id>:after_id AND i.expires_at<=CAST(:cutoff AS timestamptz)
                      AND NOT """ + IDEMPOTENCY_LIVE_SQL + """
                    ORDER BY i.id LIMIT :limit FOR UPDATE OF b SKIP LOCKED
                """), params)
                ids = [int(row["id"]) for row in selected.mappings()]
                if not ids:
                    return CleanupPage(0, cursor)
                # 只从精确关联且指纹一致的结果保留到期时间，不猜测修复历史孤儿。
                await connection.execute(text("""
                    UPDATE idempotency_claim q SET result_expires_at=i.expires_at
                    FROM idempotency_record i
                    WHERE i.id=ANY(:ids) AND q.state='completed' AND q.batch_id=i.batch_id
                      AND q.scope_kind=i.scope_kind AND q.scope_id=i.scope_id AND q.biz_id=i.biz_id
                      AND trim(q.fingerprint)=trim(i.request_hash)
                      AND i.request_hash_key_version IS NOT NULL
                """), {"ids": ids})
                deleted = await connection.execute(text("""
                    DELETE FROM idempotency_record i USING sms_batch b
                    WHERE i.id=ANY(:ids) AND i.batch_id=b.id
                      AND i.expires_at<=CAST(:cutoff AS timestamptz) AND NOT
                """ + IDEMPOTENCY_LIVE_SQL + " RETURNING i.id"), {"ids": ids, "cutoff": cutoff})
                count = len(list(deleted.mappings()))
                return CleanupPage(count, (max(ids),), CleanupCounts(idempotency=count))
        finally:
            await engine.dispose()
