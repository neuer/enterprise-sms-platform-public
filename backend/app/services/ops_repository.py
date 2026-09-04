"""运维中心只读安全元数据的 PostgreSQL 仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.jobtrack import JobSpec
from app.core.runtime_resources import (
    bind_connection_audit_subject,
    bind_connection_system_audit,
    database_engine,
)
from app.services.ops import (
    AlertLevel,
    AlertQuery,
    AlertRecord,
    JobRecord,
    OpsPage,
    PausedBatch,
    QueueSnapshot,
    RawLogQuery,
    RawLogRecord,
    RawSource,
    UncertainRecord,
    UnmatchedQuery,
    UnmatchedRecord,
)
from app.services.queue_pause import parse_queue_pause_claim
from app.services.raw_lease import (
    CLAIM_LEASE_PREDICATE_SQL,
    CLAIM_LEASE_SET_SQL,
    FENCED_TERMINAL_SQL,
    RAW_LEASE_SECONDS,
    STALE_LEASE_PREDICATE_SQL,
    SYSTEM_REPLAY_AUDIT_COMPLETED,
    SYSTEM_REPLAY_AUDIT_PENDING,
    RawLeaseLost,
    RawProcessingLease,
    commit_fenced_raw_update,
    lease_from_row,
    new_lease_id,
    require_lease,
)
from app.services.raw_parse import (
    RawParseDisposition,
    claim_eligibilities,
    mark_error_column_values,
)
from app.services.raw_replay import (
    MAX_RAW_REPLAY_ATTEMPTS,
    RawReplayClaim,
    RawReplayConflict,
    RawReplayRecord,
)
from app.settings import Settings, get_settings


def _is_unique_violation(error: BaseException) -> bool:
    """只认 PostgreSQL unique_violation，避免把触发器/载荷约束当幂等。"""

    current: object | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "pgcode", None) or getattr(current, "sqlstate", None)
        if code == "23505":
            return True
        nxt = getattr(current, "orig", None)
        if nxt is None and isinstance(current, BaseException):
            nxt = current.__cause__ or current.__context__
        current = nxt
    return False


class SqlOpsRepository:
    """查询绝不选择 raw 密文或 unmatched 号码保护字段。"""

    def __init__(self, settings: Settings | None = None, redis: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis: Any = redis or Redis.from_url(
            self.settings.redis_control_url,
            decode_responses=True,
        )

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_jobs(
        self,
        specs: Sequence[JobSpec],
        *,
        now: datetime,
    ) -> tuple[JobRecord, ...]:
        if not specs:
            return ()
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH specs AS (
                          SELECT * FROM unnest(
                            CAST(:job_names AS text[]),CAST(:intervals AS integer[])
                          ) AS value(job_name,expect_interval_s)
                        ), latest AS (
                          SELECT DISTINCT ON (job_name) job_name,started_at,status,
                            duration_ms,items
                          FROM job_run ORDER BY job_name,started_at DESC,id DESC
                        ), stats AS (
                          SELECT job_name,
                            count(*) FILTER (WHERE status='success') success_count,
                            count(*) FILTER (WHERE status='failed') failed_count
                          FROM job_run
                          WHERE started_at>=CAST(:day_start AS timestamptz)
                            AND status IN ('success','failed')
                          GROUP BY job_name
                        )
                        SELECT s.job_name,l.started_at last_run_at,
                          l.status last_status,l.duration_ms last_duration_ms,
                          COALESCE(l.items,0) last_items,
                          COALESCE(
                            st.success_count::double precision /
                            NULLIF(st.success_count+st.failed_count,0),0
                          ) success_rate_24h,
                          (l.started_at IS NULL OR l.started_at < CAST(:now AS timestamptz) -
                            make_interval(secs=>s.expect_interval_s*2)) stalled
                        FROM specs s LEFT JOIN latest l USING(job_name)
                        LEFT JOIN stats st USING(job_name)
                        ORDER BY s.job_name
                        """
                    ),
                    {
                        "job_names": [spec.job_name for spec in specs],
                        "intervals": [spec.expect_interval_s for spec in specs],
                        "now": now,
                        "day_start": now - timedelta(hours=24),
                    },
                )
                return tuple(
                    JobRecord(
                        str(row["job_name"]),
                        row["last_run_at"],
                        str(row["last_status"]) if row["last_status"] is not None else None,
                        int(row["last_duration_ms"])
                        if row["last_duration_ms"] is not None
                        else None,
                        int(row["last_items"]),
                        float(row["success_rate_24h"]),
                        bool(row["stalled"]),
                    )
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()

    async def audit_job_trigger(
        self,
        job_name: str,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> None:
        """手动触发审计必须用接口已验证的人类主体显式绑定后再落库。

        独立 `engine.begin()` 不能只靠 begin 事件里的 ContextVar 签名：
        事务 XID 或主体上下文一旦没带进 INSERT，`enforce_live_audit_principal`
        会以 check violation 拒绝 `legacy_unknown` 行，接口就变成 500。
        这里按其它写路径显式 bind + insert_audit；主体来自 JWT，不回读 ContextVar。
        """

        if principal.actor_name != actor:
            raise RuntimeError("job trigger audit principal unavailable")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        ip=ip,
                        action="job_trigger",
                        object_type="job",
                        object_id=job_name,
                        after={"status": "requested"},
                    ),
                )
        finally:
            await engine.dispose()

    async def queue_snapshot(self) -> QueueSnapshot:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT balance FROM balance_snapshot
                           ORDER BY fetched_at DESC,id DESC LIMIT 1) balance,
                          COALESCE((SELECT value::integer FROM sys_config
                            WHERE key='balance_alert_threshold'),10000) threshold
                        """
                    )
                )
                row = result.mappings().one()
        finally:
            await engine.dispose()
        values = await self.redis.mget(
            "queue:paused:realtime",
            "queue:paused:bulk",
        )
        return QueueSnapshot(
            parse_queue_pause_claim(values[0]),
            parse_queue_pause_claim(values[1]),
            int(row["balance"]) if row["balance"] is not None else None,
            int(row["threshold"]),
        )

    async def resume_batches(
        self,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
    ) -> tuple[PausedBatch, ...]:
        """队列恢复与审计必须同事务：显式绑定人类主体后再改状态并落库。

        独立 `engine.begin()` 不能只靠 begin 事件里的 ContextVar 签名。
        主体来自已验证 JWT，与 actor 不一致时在 UPDATE 之前 fail closed。
        """

        if principal.actor_name != actor:
            raise RuntimeError("queue resume audit principal unavailable")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                result = await connection.execute(
                    text(
                        """
                        UPDATE sms_batch SET status='queued',updated_at=now()
                        WHERE status='balance_blocked'
                        RETURNING trim(batch_no) batch_no,category
                        """
                    )
                )
                batches = tuple(
                    PausedBatch(str(row["batch_no"]), str(row["category"]))
                    for row in result.mappings()
                )
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        ip=ip,
                        action="queue_resume",
                        object_type="queue",
                        object_id="all",
                        after={"resumed_batches": len(batches)},
                    ),
                )
                return batches
        finally:
            await engine.dispose()

    async def clear_queue_pauses(self) -> None:
        await self.redis.delete("queue:paused:realtime", "queue:paused:bulk")

    async def list_stale_unprocessed_raw_ids(
        self,
        *,
        limit: int = 20,
        max_attempts: int = MAX_RAW_REPLAY_ATTEMPTS,
    ) -> list[int]:
        """列出租约过期或尚未开始处理的未处理 raw，供自动重放。

        尝试次数少者优先，防止最老的永久毒丸垄断整个 LIMIT 窗口；达到
        上限的 raw 退出自动重放，仅保留人工重放入口。
        """

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        SELECT id FROM raw_vendor_log
                        WHERE processed=false
                          AND capture_state='complete'
                          AND replay_eligibility='automatic'
                          AND replay_attempts<:max_attempts
                          AND (
                            {STALE_LEASE_PREDICATE_SQL.strip()}
                          )
                        ORDER BY replay_attempts,id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit, "max_attempts": max_attempts},
                )
                return [int(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def list_pending_system_replay_audit_ids(self, *, limit: int = 20) -> list[int]:
        """已处理但系统审计未完成的 raw，只补审计、不再认领。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id FROM raw_vendor_log
                        WHERE processed=true
                          AND system_replay_audit_state=:pending
                        ORDER BY id
                        LIMIT :limit
                        """
                    ),
                    {"pending": SYSTEM_REPLAY_AUDIT_PENDING, "limit": limit},
                )
                return [int(value) for value in result.scalars()]
        finally:
            await engine.dispose()

    async def claim_raw_for_replay(
        self, raw_id: int, *, allow_manual: bool = True
    ) -> RawReplayClaim | None:
        allowed = claim_eligibilities(allow_manual=allow_manual)
        eligibility_sql = ", ".join(f"'{item}'" for item in allowed)
        capture_sql = (
            "'complete','complete_too_large'" if allow_manual else "'complete'"
        )
        lease_id = new_lease_id()
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        f"""
                        WITH claimed AS (
                          UPDATE raw_vendor_log
                          SET {CLAIM_LEASE_SET_SQL.strip()},
                            replay_attempts=replay_attempts+1
                          WHERE id=:raw_id AND processed=false
                            AND capture_state IN ({capture_sql})
                            AND replay_eligibility IN ({eligibility_sql})
                            AND (
                            {CLAIM_LEASE_PREDICATE_SQL.strip()}
                            )
                          RETURNING id,source,payload_enc,payload_sha256,
                            key_version,processed,item_count,http_status,
                            content_encoding,capture_state,parse_state,
                            replay_eligibility,error,processing_lease_id,
                            processing_lease_epoch,processing_lease_expires_at,
                            system_replay_audit_state
                        )
                        SELECT id,source,payload_enc,payload_sha256,key_version,
                          processed,item_count,http_status,content_encoding,
                          capture_state,parse_state,replay_eligibility,error,
                          processing_lease_id,processing_lease_epoch,
                          processing_lease_expires_at,system_replay_audit_state,
                          true claimed
                        FROM claimed
                        UNION ALL
                        SELECT id,source,payload_enc,payload_sha256,key_version,
                          processed,item_count,http_status,content_encoding,
                          capture_state,parse_state,replay_eligibility,error,
                          processing_lease_id,processing_lease_epoch,
                          processing_lease_expires_at,system_replay_audit_state,
                          false claimed
                        FROM raw_vendor_log
                        WHERE id=:raw_id
                          AND NOT EXISTS (SELECT 1 FROM claimed)
                        LIMIT 1
                        """
                    ),
                    {
                        "raw_id": raw_id,
                        "lease_id": str(lease_id),
                        "lease_seconds": RAW_LEASE_SECONDS,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                claimed = bool(row["claimed"])
                lease = None
                if claimed:
                    lease = lease_from_row(row) or RawProcessingLease(
                        int(row["id"]), lease_id, 1
                    )
                return RawReplayClaim(
                    record=self._raw_replay_record(row),
                    claimed=claimed,
                    lease=lease,
                )
        finally:
            await engine.dispose()

    def _raw_replay_record(self, row: Any) -> RawReplayRecord:
        return RawReplayRecord(
            int(row["id"]),
            str(row["source"]),
            bytes(row["payload_enc"]),
            str(row["payload_sha256"]),
            int(row["key_version"]),
            bool(row["processed"]),
            int(row["http_status"]),
            str(row["content_encoding"]),
            str(row.get("capture_state") or "complete"),
            int(row["item_count"]) if row.get("item_count") is not None else 0,
            str(row.get("parse_state") or "unattempted"),
            str(row.get("replay_eligibility") or "manual"),
            str(row["error"]) if row.get("error") is not None else None,
            str(row["system_replay_audit_state"])
            if row.get("system_replay_audit_state") is not None
            else None,
            int(row["processing_lease_epoch"] or 0)
            if row.get("processing_lease_epoch") is not None
            else 0,
        )

    async def raw_replay_exhausted(
        self,
        raw_id: int,
        *,
        max_attempts: int = MAX_RAW_REPLAY_ATTEMPTS,
    ) -> bool:
        """该未处理 raw 是否已耗尽自动重放次数（告警一次并转人工）。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.scalar(
                    text(
                        """
                        SELECT replay_attempts>=:max_attempts FROM raw_vendor_log
                        WHERE id=:raw_id AND processed=false
                        """
                    ),
                    {"raw_id": raw_id, "max_attempts": max_attempts},
                )
                return bool(result)
        finally:
            await engine.dispose()

    async def mark_replay_error(
        self,
        raw_id: int,
        error: str,
        *,
        lease: RawProcessingLease | None = None,
    ) -> None:
        message = error[:256]
        columns = mark_error_column_values(message)
        token = require_lease(lease, raw_id)
        engine = self._engine()
        try:
            await commit_fenced_raw_update(
                engine,
                FENCED_TERMINAL_SQL,
                {
                    "id": raw_id,
                    "processed": False,
                    "error": message,
                    "parse_state": columns["parse_state"],
                    "replay_eligibility": columns["replay_eligibility"],
                    "lease_id": str(token.lease_id),
                    "epoch": token.epoch,
                },
                lease=token,
            )
        finally:
            await engine.dispose()

    async def load_raw_for_reevaluate(self, raw_id: int) -> RawReplayRecord | None:
        """读取 raw 供 parser 升级重评估：不占租约，不增加 replay_attempts。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,source,payload_enc,payload_sha256,key_version,
                          processed,item_count,http_status,content_encoding,
                          capture_state,parse_state,replay_eligibility,error
                        FROM raw_vendor_log
                        WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                return self._raw_replay_record(row)
        finally:
            await engine.dispose()

    async def apply_raw_reevaluation(
        self,
        raw_id: int,
        *,
        expected_processed: bool,
        expected_parse_state: str,
        expected_eligibility: str,
        disposition: RawParseDisposition,
        error: str | None,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        """同一事务写入解析面资格与 raw_reevaluate 审计；失败全部回滚。"""

        if principal.actor_name != actor:
            raise RuntimeError("raw reevaluate audit principal unavailable")
        if expected_processed and (
            disposition.parse_state != "processed"
            or disposition.replay_eligibility != "never"
        ):
            raise RawReplayConflict("已处理 raw 不得改回可重放状态")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                current = (
                    await connection.execute(
                        text(
                            """
                            SELECT processed,parse_state,replay_eligibility,
                              (
                                processing_lease_id IS NOT NULL
                                AND processing_lease_expires_at>now()
                              ) AS live_lease
                            FROM raw_vendor_log
                            WHERE id=:raw_id
                            FOR UPDATE
                            """
                        ),
                        {"raw_id": raw_id},
                    )
                ).mappings().one_or_none()
                if current is None:
                    raise RawReplayConflict("原始报文不存在")
                already_applied = (
                    bool(current["processed"]) == expected_processed
                    and str(current["parse_state"]) == disposition.parse_state
                    and str(current["replay_eligibility"])
                    == disposition.replay_eligibility
                )
                if already_applied:
                    existing_audit = await connection.scalar(
                        text(
                            """
                            SELECT 1 FROM audit_log
                            WHERE action='raw_reevaluate'
                              AND object_type='raw_vendor_log'
                              AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                            LIMIT 1
                            """
                        ),
                        {"raw_id": raw_id},
                    )
                    if existing_audit is not None:
                        return
                if bool(current["live_lease"]):
                    raise RawLeaseLost("raw processing lease held")
                result = await connection.execute(
                    text(
                        """
                        UPDATE raw_vendor_log
                        SET parse_state=:parse_state,
                            replay_eligibility=:replay_eligibility,
                            error=:error
                        WHERE id=:raw_id
                          AND processed=:expected_processed
                          AND parse_state=:expected_parse_state
                          AND replay_eligibility=:expected_eligibility
                          AND (
                            processing_lease_id IS NULL
                            OR processing_lease_expires_at IS NULL
                            OR processing_lease_expires_at<=now()
                          )
                        """
                    ),
                    {
                        "raw_id": raw_id,
                        "parse_state": disposition.parse_state,
                        "replay_eligibility": disposition.replay_eligibility,
                        "error": error,
                        "expected_processed": expected_processed,
                        "expected_parse_state": expected_parse_state,
                        "expected_eligibility": expected_eligibility,
                    },
                )
                if getattr(result, "rowcount", 1) == 0:
                    raise RawReplayConflict("raw 状态已变化，请刷新后重试")
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        ip=ip,
                        action="raw_reevaluate",
                        object_type="raw_vendor_log",
                        object_id=str(raw_id),
                        before=before,
                        after=after,
                    ),
                )
        finally:
            await engine.dispose()

    async def has_human_raw_replay_audit(self, raw_id: int) -> bool:
        """该 raw 是否已有 raw_replay 审计事实（人类补写前的结果边界）。

        系统重放若已落 system 审计，人类路径不得再补写或混用主体。
        """

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                found = await connection.scalar(
                    text(
                        """
                        SELECT 1 FROM audit_log
                        WHERE action='raw_replay'
                          AND object_type='raw_vendor_log'
                          AND object_id=CAST(CAST(:raw_id AS bigint) AS text)
                        LIMIT 1
                        """
                    ),
                    {"raw_id": raw_id},
                )
                return found is not None
        finally:
            await engine.dispose()

    async def audit_raw_replay(
        self,
        raw_id: int,
        *,
        source: str,
        items: int,
        actor: str,
        ip: str,
        system_producer: bool = False,
        principal: SecurityPrincipal | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        """记录 raw 重放审计。

        人类路径必须用接口已验证的 SecurityPrincipal 显式绑定后再
        insert_audit；禁止只写 actor 字符串。后台任务（reconcile
        自动重放）没有认证会话，必须绑定 system 生产者上下文并以
        actor_subject_kind='system' 落库，且不得混入人类主体。
        """

        if system_producer:
            if principal is not None:
                raise RuntimeError("system raw replay cannot bind a human principal")
        elif principal is None or principal.actor_name != actor:
            raise RuntimeError("raw replay audit principal unavailable")
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                if system_producer:
                    epoch = lease_epoch
                    if epoch is None:
                        epoch = int(
                            await connection.scalar(
                                text(
                                    """
                                    SELECT processing_lease_epoch
                                    FROM raw_vendor_log WHERE id=:raw_id
                                    """
                                ),
                                {"raw_id": raw_id},
                            )
                            or 0
                        )
                    producer_domain = self.settings.audit_producer_domain or "realtime"
                    await bind_connection_system_audit(
                        connection,
                        actor_name=actor,
                        action="raw_replay",
                        producer_domain=producer_domain,
                    )
                    # sms_send 只有 audit_log INSERT，没有 SELECT。冲突推断
                    # 会触发表级读权限检查；重复写入只吞 unique_violation。
                    try:
                        async with connection.begin_nested():
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO audit_log(
                                      actor,actor_subject_kind,role,action,object_type,
                                      object_id,after_val
                                    ) VALUES (
                                      :actor,'system','system','raw_replay',
                                      'raw_vendor_log',
                                      CAST(CAST(:raw_id AS bigint) AS text),
                                      jsonb_build_object(
                                        'source',CAST(:source AS text),
                                        'items',CAST(:items AS integer),
                                        'lease_epoch',CAST(:lease_epoch AS bigint),
                                        'producer_domain',CAST(:producer_domain AS text)
                                      )
                                    )
                                    """
                                ),
                                {
                                    "actor": actor,
                                    "raw_id": raw_id,
                                    "source": source,
                                    "items": items,
                                    "lease_epoch": epoch,
                                    "producer_domain": producer_domain,
                                },
                            )
                    except IntegrityError as error:
                        if not _is_unique_violation(error):
                            raise
                    completed = await connection.execute(
                        text(
                            """
                            UPDATE raw_vendor_log
                            SET system_replay_audit_state=:completed
                            WHERE id=:raw_id
                              AND system_replay_audit_state=:pending
                            """
                        ),
                        {
                            "raw_id": raw_id,
                            "pending": SYSTEM_REPLAY_AUDIT_PENDING,
                            "completed": SYSTEM_REPLAY_AUDIT_COMPLETED,
                        },
                    )
                    if getattr(completed, "rowcount", 0) == 0:
                        state = await connection.scalar(
                            text(
                                """
                                SELECT system_replay_audit_state
                                FROM raw_vendor_log WHERE id=:raw_id
                                """
                            ),
                            {"raw_id": raw_id},
                        )
                        if state != SYSTEM_REPLAY_AUDIT_COMPLETED:
                            raise RuntimeError("system raw replay audit incomplete")
                    return
                assert principal is not None
                await bind_connection_audit_subject(
                    connection,
                    subject_kind="human",
                    actor_name=principal.login_name,
                    account_id=principal.account_id,
                    identity_id=principal.identity_id,
                )
                await insert_audit(
                    connection,
                    AuditEvent(
                        principal=principal,
                        role=principal.role,
                        ip=ip,
                        action="raw_replay",
                        object_type="raw_vendor_log",
                        object_id=str(raw_id),
                        after={"source": source, "items": items},
                    ),
                )
        finally:
            await engine.dispose()

    async def list_alerts(self, query: AlertQuery) -> OpsPage[AlertRecord]:
        where = """
          (CAST(:alert_type AS varchar(32)) IS NULL OR alert_type=:alert_type)
          AND (CAST(:level AS varchar(8)) IS NULL OR level=:level)
          AND (CAST(:start AS timestamptz) IS NULL OR created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR created_at<=:end)
        """
        params = self._page_params(query.page, query.page_size) | {
            "alert_type": query.alert_type,
            "level": query.level,
            "start": query.start,
            "end": query.end,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM alert_log WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,alert_type,level,title,detail,channels,created_at "
                        "FROM alert_log WHERE "
                        + where
                        + " ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    AlertRecord(
                        int(row["id"]),
                        str(row["alert_type"]),
                        cast(AlertLevel, str(row["level"])),
                        str(row["title"]),
                        cast(dict[str, Any] | None, row["detail"]),
                        str(row["channels"]),
                        row["created_at"],
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    async def list_raw_logs(self, query: RawLogQuery) -> OpsPage[RawLogRecord]:
        where = """
          (CAST(:source AS varchar(8)) IS NULL OR source=:source)
          AND (CAST(:processed AS boolean) IS NULL OR processed=:processed)
        """
        params = self._page_params(query.page, query.page_size) | {
            "source": query.source,
            "processed": query.processed,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM raw_vendor_log WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,source,item_count,cardinality(custom_ids) custom_id_count,"
                        "processed,error,fetched_at,capture_state,parse_state,"
                        "replay_eligibility FROM raw_vendor_log WHERE "
                        + where
                        + " ORDER BY fetched_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    RawLogRecord(
                        int(row["id"]),
                        cast(RawSource, str(row["source"])),
                        int(row["item_count"]),
                        int(row["custom_id_count"]),
                        bool(row["processed"]),
                        str(row["error"]) if row["error"] is not None else None,
                        row["fetched_at"],
                        str(row.get("capture_state") or "complete"),
                        str(row.get("parse_state") or "unattempted"),
                        str(row.get("replay_eligibility") or "manual"),
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    async def list_uncertain(self, page: int, page_size: int) -> OpsPage[UncertainRecord]:
        params = self._page_params(page, page_size)
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM sms_chunk
                        WHERE status IN ('uncertain','unknown_terminal')
                        """
                    ),
                    params,
                )
                result = await connection.execute(
                    text(
                        """
                        SELECT c.id chunk_id,trim(b.batch_no) batch_no,
                          trim(c.custom_id) custom_id,c.phone_count,c.vendor_code,
                          COALESCE(c.uncertain_since,b.created_at) uncertain_since,
                          GREATEST(0,EXTRACT(EPOCH FROM (
                            now()-COALESCE(c.uncertain_since,b.created_at)
                          ))::bigint) age_seconds,
                          c.status,r.id resolution_id,r.action resolution_action,
                          r.state resolution_state,r.proposer_account_id
                        FROM sms_chunk c JOIN sms_batch b ON b.id=c.batch_id
                        LEFT JOIN sms_uncertain_resolution r ON r.chunk_id=c.id
                        WHERE c.status IN ('uncertain','unknown_terminal')
                        ORDER BY COALESCE(c.uncertain_since,b.created_at),c.id
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
                items = tuple(
                    UncertainRecord(
                        int(row["chunk_id"]),
                        str(row["batch_no"]),
                        str(row["custom_id"]),
                        int(row["phone_count"]),
                        int(row["vendor_code"]) if row["vendor_code"] is not None else None,
                        row["uncertain_since"],
                        int(row["age_seconds"]),
                        str(row["status"]),
                        int(row["resolution_id"])
                        if row.get("resolution_id") is not None
                        else None,
                        str(row["resolution_action"])
                        if row.get("resolution_action") is not None
                        else None,
                        str(row["resolution_state"])
                        if row.get("resolution_state") is not None
                        else None,
                        int(row["proposer_account_id"])
                        if row.get("proposer_account_id") is not None
                        else None,
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), page, page_size)
        finally:
            await engine.dispose()

    async def list_unmatched(self, query: UnmatchedQuery) -> OpsPage[UnmatchedRecord]:
        where = """
          (:has_phone=false OR phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[])))
          AND (CAST(:start AS timestamptz) IS NULL OR created_at>=:start)
          AND (CAST(:end AS timestamptz) IS NULL OR created_at<=:end)
        """
        params = self._page_params(query.page, query.page_size) | {
            "has_phone": bool(query.phone_hmacs),
            "phone_hmacs": list(query.phone_hmacs),
            "start": query.start,
            "end": query.end,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                count = await connection.execute(
                    text("SELECT count(*) FROM unmatched_report WHERE " + where), params
                )
                result = await connection.execute(
                    text(
                        "SELECT id,vendor_task_id,custom_id,phone_mask,report_status,"
                        "report_desc,report_time,created_at FROM unmatched_report WHERE "
                        + where
                        + " ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
                items = tuple(
                    UnmatchedRecord(
                        int(row["id"]),
                        str(row["vendor_task_id"]) if row["vendor_task_id"] is not None else None,
                        str(row["custom_id"]) if row["custom_id"] is not None else None,
                        str(row["phone_mask"]),
                        int(row["report_status"]) if row["report_status"] is not None else None,
                        str(row["report_desc"]) if row["report_desc"] is not None else None,
                        row["report_time"],
                        row["created_at"],
                    )
                    for row in result.mappings()
                )
                return OpsPage(items, int(count.scalar_one()), query.page, query.page_size)
        finally:
            await engine.dispose()

    @staticmethod
    def _page_params(page: int, page_size: int) -> dict[str, object]:
        return {"limit": page_size, "offset": (page - 1) * page_size}
