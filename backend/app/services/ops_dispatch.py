"""运维动作写入 PostgreSQL Outbox，API 无需接触 Celery broker。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import bind_connection_audit_subject, database_engine
from app.services.outbox import MANUAL_JOB_TASK_NAMES, OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.settings import Settings, get_settings


class SignAdoptionUnavailable(RuntimeError):
    """关联意图无法安全持久化。"""


class SignAdoptionConflict(RuntimeError):
    """签名自动绑定已进入不可取消阶段，暂不能改为关联已有签名。"""


class OutboxJobSender:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send(self, task_name: str, queue: str) -> None:
        if task_name not in MANUAL_JOB_TASK_NAMES:
            raise ValueError("manual job task is not allowlisted")
        job_name = task_name.rsplit(".", 1)[-1]
        request_id = uuid4()
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="job.trigger",
                        aggregate_type="job",
                        aggregate_id=job_name,
                        task_name="app.tasks.outbox.trigger_job",
                        queue=queue,
                        args=(task_name,),
                        dedup_key=f"job.trigger:{job_name}:{request_id}",
                    ),
                )
        finally:
            await engine.dispose()


class TemplateSyncSender:
    """把已授权模板主键固化为可合并的精确同步意图。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def send_template(
        self,
        template_id: int,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> None:
        if isinstance(template_id, bool) or template_id <= 0:
            raise ValueError("invalid template sync id")
        minute_bucket = int(self.clock().timestamp()) // 60
        engine = database_engine(self.settings.database_url)
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
                        action="template_sync",
                        object_type="template",
                        object_id=str(template_id),
                        after={"status": "requested"},
                    ),
                )
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="template.sync",
                        aggregate_type="sms_template",
                        aggregate_id=str(template_id),
                        task_name="app.tasks.sync_template",
                        queue="realtime",
                        args=(template_id,),
                        dedup_key=f"template.sync:{template_id}:{minute_bucket}",
                        max_attempts=3,
                    ),
                )
        finally:
            await engine.dispose()


class SignSyncSender:
    """把管理员授权的签名同步请求与人类审计原子持久化。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def send_sign(
        self,
        sign_id: int,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> None:
        if isinstance(sign_id, bool) or not 1 <= sign_id <= 2_147_483_647:
            raise ValueError("invalid sign sync id")
        minute_bucket = int(self.clock().timestamp()) // 60
        engine = database_engine(self.settings.database_url)
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
                        action="sign_sync",
                        object_type="sign",
                        object_id=str(sign_id),
                        after={"status": "requested"},
                    ),
                )
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="job.trigger",
                        aggregate_type="sms_sign",
                        aggregate_id=str(sign_id),
                        task_name="app.tasks.outbox.trigger_job",
                        queue="realtime",
                        args=("app.tasks.sync_signs", sign_id),
                        dedup_key=(
                            f"job.trigger:sync_signs:{sign_id}:{minute_bucket}"
                        ),
                        max_attempts=3,
                    ),
                )
        finally:
            await engine.dispose()


class SignAdoptionSender:
    """把管理员核对过的本地签名与厂商 ID 固化为精确 Outbox 意图。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send_sign(
        self,
        sign_id: int,
        vendor_sign_id: int,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> None:
        if (
            isinstance(sign_id, bool)
            or sign_id <= 0
            or isinstance(vendor_sign_id, bool)
            or vendor_sign_id <= 0
            or vendor_sign_id > 2_147_483_647
        ):
            raise ValueError("invalid sign adoption reference")
        request_id = uuid4()
        try:
            engine = database_engine(self.settings.database_url)
            try:
                async with engine.begin() as connection:
                    await bind_connection_audit_subject(
                        connection,
                        subject_kind="human",
                        actor_name=principal.login_name,
                        account_id=principal.account_id,
                        identity_id=principal.identity_id,
                    )
                    superseded_bind = await self._reserve_adoption(
                        connection,
                        sign_id,
                    )
                    await insert_audit(
                        connection,
                        AuditEvent(
                            principal=principal,
                            role=principal.role,
                            ip=ip,
                            action="sign_adopt",
                            object_type="sign",
                            object_id=str(sign_id),
                            after={
                                "status": "requested",
                                "vendor_sign_id": vendor_sign_id,
                                "pending_bind_superseded": superseded_bind,
                            },
                        ),
                    )
                    await enqueue_outbox(
                        connection,
                        OutboxEventSpec(
                            event_type="sign.adopt",
                            aggregate_type="sms_sign",
                            aggregate_id=str(sign_id),
                            task_name="app.tasks.adopt_sign",
                            queue="realtime",
                            args=(sign_id, vendor_sign_id),
                            dedup_key=f"sign.adopt:{sign_id}:{request_id}",
                            max_attempts=3,
                        ),
                    )
            finally:
                await engine.dispose()
        except SQLAlchemyError as error:
            raise SignAdoptionUnavailable(
                "sign adoption persistence unavailable"
            ) from error

    @staticmethod
    async def _reserve_adoption(connection: Any, sign_id: int) -> bool:
        """锁定签名并只撤销尚未发布的自动 BindSign，杜绝双路竞态。"""

        current = await connection.execute(
            text(
                """
                SELECT vendor_state,vendor_sign_id FROM sms_sign
                WHERE id=:id FOR UPDATE
                """
            ),
            {"id": sign_id},
        )
        row = current.mappings().one_or_none()
        if (
            row is None
            or str(row["vendor_state"]) != "pending"
            or row["vendor_sign_id"] is not None
        ):
            raise SignAdoptionConflict("签名状态或厂商编号已变化，请刷新后重试")

        active_result = await connection.execute(
            text(
                """
                SELECT event_type,state FROM outbox_event
                WHERE aggregate_type='sms_sign' AND aggregate_id=:aggregate_id
                  AND event_type IN ('sign.bind','sign.adopt')
                  AND state IN ('pending','leased','published','processing')
                ORDER BY created_at
                FOR UPDATE
                """
            ),
            {"aggregate_id": str(sign_id)},
        )
        active = list(active_result.mappings())
        if any(item["event_type"] == "sign.adopt" for item in active):
            raise SignAdoptionConflict("该签名已有厂商关联任务正在处理")
        if any(
            item["event_type"] == "sign.bind" and item["state"] != "pending"
            for item in active
        ):
            raise SignAdoptionConflict("签名自动提交正在执行，请稍后刷新")

        pending_bind = any(item["event_type"] == "sign.bind" for item in active)
        if pending_bind:
            await connection.execute(
                text(
                    """
                    UPDATE outbox_event SET state='completed',completed_at=now(),
                      updated_at=now()
                    WHERE aggregate_type='sms_sign' AND aggregate_id=:aggregate_id
                      AND event_type='sign.bind' AND state='pending'
                    """
                ),
                {"aggregate_id": str(sign_id)},
            )
        return pending_bind


class OutboxBatchSender:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send_batch(self, batch_no: str, lane: str) -> None:
        request_id = uuid4()
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.begin() as connection:
                await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        event_type="batch.ready",
                        aggregate_type="sms_batch",
                        aggregate_id=batch_no,
                        task_name="app.tasks.send.process_batch",
                        queue=lane,
                        args=(batch_no,),
                        dedup_key=f"batch.ready:{request_id.hex}",
                    ),
                )
        finally:
            await engine.dispose()
