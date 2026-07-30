"""审批单 PostgreSQL 状态迁移与查询。"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import database_engine
from app.services.approval import ApprovalCase
from app.services.callback_repository import enqueue_batch_finished
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.usage_ledger import request_usage_release_for_batch
from app.settings import Settings, get_settings

CASE_SELECT = """
SELECT p.id approval_id,trim(b.batch_no) batch_no,p.applicant,
  p.applicant_account_id,p.applicant_identity_id,b.app_id,b.dept,
  to_char(b.created_at AT TIME ZONE 'Asia/Shanghai','YYYYMMDD') quota_date,
  b.quota_cost,b.category,p.status,b.status batch_status
FROM approval p JOIN sms_batch b ON b.id=p.batch_id
"""
LOGGER = logging.getLogger(__name__)


async def record_pending_approval_alert(
    connection: Any,
    *,
    batch_no: str,
    dept: str,
    category: str,
    total: int,
) -> None:
    """以无 PII log-sink 记录待审批通知，空渠道环境绝不外呼。"""

    await connection.execute(
        text(
            """
            INSERT INTO alert_log(
              alert_type,level,title,detail,channels,dedup_key
            ) VALUES(
              'approval_pending','info','存在新的短信审批待办',
              CAST(:detail AS jsonb),:channels,:dedup_key
            )
            """
        ),
        {
            "detail": json.dumps(
                {
                    "batch_no": batch_no,
                    "dept": dept,
                    "category": category,
                    "total": total,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "channels": "log-sink",
            "dedup_key": f"approval_pending:{batch_no}",
        },
    )


def _case(row: Any, *, outbox_persisted: bool = False) -> ApprovalCase:
    return ApprovalCase(
        int(row["approval_id"]),
        str(row["batch_no"]),
        str(row["applicant"]),
        int(row["app_id"] or 0),
        str(row["dept"]),
        str(row["quota_date"]),
        int(row["quota_cost"]),
        str(row["category"]),
        str(row["status"]),
        str(row["batch_status"]),
        (int(row["applicant_account_id"]) if row["applicant_account_id"] is not None else None),
        (int(row["applicant_identity_id"]) if row["applicant_identity_id"] is not None else None),
        outbox_persisted,
    )


class SqlApprovalRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def list_page(
        self,
        *,
        status: str,
        dept: str | None,
        page: int,
        size: int = 20,
    ) -> dict[str, object]:
        dept_filter = "" if dept is None else "AND p.dept=:dept"
        params: dict[str, object] = {
            "status": status,
            "dept": dept,
            "limit": size,
            "offset": (page - 1) * size,
        }
        source = f"""
          FROM approval p JOIN sms_batch b ON b.id=p.batch_id
          WHERE p.status=:status {dept_filter}
        """
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                total = await connection.scalar(text(f"SELECT count(*) {source}"), params)
                result = await connection.execute(
                    text(
                        f"""
                        SELECT p.id,trim(b.batch_no) batch_no,b.category,p.applicant,p.dept,
                          b.total,b.segments,b.quota_cost estimated_segments,
                          b.scheduled_at,p.trigger_threshold,
                          p.trigger_threshold_source,b.content,p.status,
                          p.approver,p.reason,p.created_at
                        {source} ORDER BY p.created_at DESC LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
                return {"total": int(total or 0), "items": [dict(row) for row in result.mappings()]}
        finally:
            await engine.dispose()

    async def get(self, approval_id: int) -> ApprovalCase | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"{CASE_SELECT} WHERE p.id=:id"), {"id": approval_id}
                )
                row = result.mappings().one_or_none()
                return _case(row) if row is not None else None
        finally:
            await engine.dispose()

    async def transition(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
    ) -> ApprovalCase | None:
        approval_status = "approved" if action == "approve" else "rejected"
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        UPDATE approval SET
                          status=:approval_status,
                          approver=:approver,
                          approver_account_id=:approver_account_id,
                          approver_identity_id=:approver_identity_id,
                          reason=:reason,decided_at=now()
                        WHERE id=:id AND status='pending'
                          AND applicant_account_id IS NOT NULL
                          AND applicant_account_id<>:approver_account_id
                        RETURNING batch_id
                        """
                    ),
                    {
                        "id": approval_id,
                        "approval_status": approval_status,
                        "approver": principal.login_name,
                        "approver_account_id": principal.account_id,
                        "approver_identity_id": principal.identity_id,
                        "reason": reason,
                    },
                )
                batch_id = result.scalar_one_or_none()
                if batch_id is None:
                    return None
                await connection.execute(
                    text(
                        """
                        UPDATE sms_batch SET status=CASE WHEN :action='approve'
                          THEN CASE WHEN scheduled_at IS NULL THEN 'queued' ELSE 'scheduled' END
                          ELSE 'rejected' END,updated_at=now() WHERE id=:batch_id
                        """
                    ),
                    {"action": action, "batch_id": batch_id},
                )
                if action == "reject":
                    await enqueue_batch_finished(connection, int(batch_id))
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_account_id,actor_identity_id,
                          role,action,object_type,object_id,after_val
                        )
                        VALUES(
                          :actor,'human',:actor_account_id,:actor_identity_id,
                          :role,'approval_decision','approval',
                          CAST(CAST(:id AS bigint) AS text),
                          jsonb_build_object(
                            'actor_account_id',CAST(:actor_account_id AS bigint),
                            'actor_identity_id',CAST(:actor_identity_id AS bigint),
                            'decision',CAST(:decision AS text)
                          ))
                        """
                    ),
                    {
                        "actor": principal.login_name,
                        "actor_account_id": principal.account_id,
                        "actor_identity_id": principal.identity_id,
                        "role": principal.role,
                        "id": approval_id,
                        "decision": action,
                    },
                )
                current = await connection.execute(
                    text(f"{CASE_SELECT} WHERE p.id=:id"), {"id": approval_id}
                )
                row = current.mappings().one()
                case = _case(row, outbox_persisted=True)
                if action == "approve" and case.batch_status == "queued":
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="batch.ready",
                            aggregate_type="sms_batch",
                            aggregate_id=case.batch_no,
                            task_name="app.tasks.send.process_batch",
                            queue=("bulk" if case.category == "market" else "realtime"),
                            args=(case.batch_no,),
                            dedup_key=f"approval:{approval_id}:approved",
                        ),
                    )
                if action == "reject":
                    release_event = f"approval:{approval_id}:rejected"
                    if not await request_usage_release_for_batch(
                        connection,
                        batch_id=int(batch_id),
                        event_id=release_event,
                    ):
                        await enqueue_outbox(
                            connection,
                            OutboxEventSpec(
                                event_type="quota.compensation",
                                aggregate_type="approval",
                                aggregate_id=str(approval_id),
                                task_name="app.tasks.outbox.compensate_quota",
                                queue="realtime",
                                args=(
                                    case.app_id,
                                    case.dept,
                                    case.category,
                                    case.quota_date,
                                    case.quota_cost,
                                    release_event,
                                ),
                                dedup_key=release_event,
                            ),
                        )
                return case
        finally:
            await engine.dispose()

    async def expire_due(self) -> list[ApprovalCase]:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id FROM approval
                        WHERE status='pending' AND expires_at<=now()
                        ORDER BY expires_at,id LIMIT 500
                        """
                    )
                )
                approval_ids = [int(value) for value in result.scalars()]
            cases: list[ApprovalCase] = []
            for approval_id in approval_ids:
                completed_case: ApprovalCase | None = None
                try:
                    async with engine.begin() as connection:
                        result = await connection.execute(
                            text(
                                """
                                UPDATE approval p SET status='expired',decided_at=now()
                                FROM sms_batch b
                                WHERE b.id=p.batch_id AND p.id=:approval_id
                                  AND p.status='pending' AND p.expires_at<=now()
                                RETURNING b.id batch_id,p.id approval_id,
                                  trim(b.batch_no) batch_no,p.applicant,
                                  p.applicant_account_id,p.applicant_identity_id,
                                  b.app_id,b.dept,
                                  to_char(
                                    b.created_at AT TIME ZONE 'Asia/Shanghai',
                                    'YYYYMMDD'
                                  ) quota_date,
                                  b.quota_cost,b.category,p.status,b.status batch_status
                                """
                            ),
                            {"approval_id": approval_id},
                        )
                        row = result.mappings().one_or_none()
                        if row is None:
                            continue
                        case = _case(row, outbox_persisted=True)
                        await connection.execute(
                            text(
                                """
                                UPDATE sms_batch SET status='expired',updated_at=now()
                                WHERE id=:batch_id
                                """
                            ),
                            {"batch_id": int(row["batch_id"])},
                        )
                        await enqueue_batch_finished(connection, int(row["batch_id"]))
                        release_event = f"approval:{case.approval_id}:expired"
                        if not await request_usage_release_for_batch(
                            connection,
                            batch_id=int(row["batch_id"]),
                            event_id=release_event,
                        ):
                            await enqueue_outbox(
                                connection,
                                OutboxEventSpec(
                                    event_type="quota.compensation",
                                    aggregate_type="approval",
                                    aggregate_id=str(case.approval_id),
                                    task_name="app.tasks.outbox.compensate_quota",
                                    queue="realtime",
                                    args=(
                                        case.app_id,
                                        case.dept,
                                        case.category,
                                        case.quota_date,
                                        case.quota_cost,
                                        release_event,
                                    ),
                                    dedup_key=release_event,
                                ),
                            )
                        completed_case = case
                except Exception as exc:
                    LOGGER.error(
                        "approval expiry item failed",
                        extra={
                            "approval_id": approval_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                if completed_case is not None:
                    cases.append(completed_case)
            return cases
        finally:
            await engine.dispose()
