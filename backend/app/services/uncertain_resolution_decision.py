"""unknown 提案与确认的事务边界：授权锁、业务写入和审计同生共死。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.accounts import SecurityPrincipal
from app.core.runtime_resources import ManagedAsyncEngine, bind_connection_audit_subject
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.services.uncertain_resolution import (
    RESOLUTION_ACTIONS,
    UncertainResolution,
    UncertainResolutionConflict,
    UncertainResolutionForbidden,
    UncertainResolutionNotFound,
    _lock_resolution_source,
    _row,
)
from app.services.usage_subject import require_source_dept


async def _bind_resolution_actor(
    connection: AsyncConnection, principal: SecurityPrincipal
) -> None:
    """把已验证人类主体绑定到当前业务事务，失败时与处置一起回滚。"""

    await bind_connection_audit_subject(
        connection,
        subject_kind="human",
        actor_name=principal.login_name,
        account_id=principal.account_id,
        identity_id=principal.identity_id,
    )


async def _require_current_admin(
    connection: AsyncConnection,
    principal: SecurityPrincipal,
    *,
    security_version: int,
    provider_code: str,
) -> None:
    """复用授权锁复核当前管理员；不接受调用方自报的 actor 字符串。"""

    if security_version < 1 or not provider_code:
        raise UncertainResolutionForbidden("管理员权限已变化，请重新登录")
    valid = (
        await connection.execute(
            text(
                """
                SELECT public.lock_business_authorization(
                    :account, :identity, :version, :provider, 'admin',
                    'admin', NULL, NULL
                )
                """
            ),
            {
                "account": principal.account_id,
                "identity": principal.identity_id,
                "version": security_version,
                "provider": provider_code,
            },
        )
    ).scalar_one()
    if valid is not True:
        raise UncertainResolutionForbidden("管理员权限已变化，请重新登录")


async def propose_resolution(
    engine: ManagedAsyncEngine,
    chunk_id: int,
    action: str,
    principal: SecurityPrincipal,
    *,
    ip: str,
    security_version: int,
    provider_code: str,
) -> UncertainResolution:
    """提案与审计同一事务；授权失效或审计失败都不留下处置单。"""

    if action not in RESOLUTION_ACTIONS:
        raise ValueError("invalid uncertain resolution action")
    try:
        async with engine.begin() as connection:
            await _bind_resolution_actor(connection, principal)
            await _require_current_admin(
                connection,
                principal,
                security_version=security_version,
                provider_code=provider_code,
            )
            chunk = (
                await connection.execute(
                    text(
                        """
                        SELECT c.id,c.batch_id
                        FROM sms_chunk c
                        WHERE c.id=:chunk_id AND c.status='unknown_terminal'
                        FOR UPDATE
                        """
                    ),
                    {"chunk_id": chunk_id},
                )
            ).mappings().one_or_none()
            if chunk is None:
                raise UncertainResolutionNotFound
            inserted = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_uncertain_resolution(
                          chunk_id,batch_id,action,proposer_account_id
                        ) VALUES (:chunk_id,:batch_id,:action,:account_id)
                        ON CONFLICT (chunk_id) DO NOTHING
                        RETURNING id,chunk_id,batch_id,action,state,
                          proposer_account_id,confirmer_account_id,
                          child_batch_id,effect_generation,effect_error
                        """
                    ),
                    {
                        "chunk_id": chunk_id,
                        "batch_id": int(chunk["batch_id"]),
                        "action": action,
                        "account_id": principal.account_id,
                    },
                )
            ).mappings().one_or_none()
            if inserted is None:
                raise UncertainResolutionConflict("该分片已有处置单")
            resolution = _row(inserted)
            await insert_audit(
                connection,
                AuditEvent(
                    principal=principal,
                    role=principal.role,
                    ip=ip,
                    action="uncertain_resolve_propose",
                    object_type="sms_uncertain_resolution",
                    object_id=str(resolution.id),
                    before={
                        "status": "unknown_terminal",
                        "chunk_id": resolution.chunk_id,
                        "batch_id": resolution.batch_id,
                    },
                    after={
                        "resolution_id": resolution.id,
                        "chunk_id": resolution.chunk_id,
                        "batch_id": resolution.batch_id,
                        "action": resolution.action,
                        "status": resolution.state,
                        "proposer_account_id": resolution.proposer_account_id,
                        "generation": resolution.effect_generation,
                    },
                ),
            )
            return resolution
    finally:
        await engine.dispose()


async def confirm_resolution(
    engine: ManagedAsyncEngine,
    resolution_id: int,
    principal: SecurityPrincipal,
    *,
    ip: str,
    security_version: int,
    provider_code: str,
) -> UncertainResolution:
    """确认、effect Outbox 与审计同一事务；失败整体回滚。"""

    try:
        async with engine.begin() as connection:
            await _bind_resolution_actor(connection, principal)
            await _require_current_admin(
                connection,
                principal,
                security_version=security_version,
                provider_code=provider_code,
            )
            await _lock_resolution_source(connection, resolution_id)
            current = (
                await connection.execute(
                    text(
                        """
                        SELECT r.id,r.chunk_id,r.batch_id,r.action,r.state,
                          r.proposer_account_id,r.confirmer_account_id,
                          r.child_batch_id,r.effect_generation,r.effect_error,
                          b.app_id,b.channel,b.category,b.dept
                        FROM sms_uncertain_resolution r
                        JOIN sms_batch b ON b.id=r.batch_id
                        WHERE r.id=:id
                        """
                    ),
                    {"id": resolution_id},
                )
            ).mappings().one_or_none()
            if current is None:
                raise UncertainResolutionNotFound
            if str(current["state"]) != "proposed":
                raise UncertainResolutionConflict("处置单已确认")
            if int(current["proposer_account_id"]) == principal.account_id:
                raise UncertainResolutionConflict("确认人不能是提案人")
            try:
                source_dept = require_source_dept(current["dept"])
            except ValueError as exc:
                raise UncertainResolutionConflict(str(exc)) from exc
            updated = (
                await connection.execute(
                    text(
                        """
                        UPDATE sms_uncertain_resolution
                        SET state='effect_pending',
                            confirmer_account_id=:account_id,
                            confirmed_at=now(),
                            approved_at=now(),
                            source_app_id=:app_id,
                            source_channel=:channel,
                            source_category=:category,
                            source_dept=:dept
                        WHERE id=:id AND state='proposed'
                        RETURNING id,chunk_id,batch_id,action,state,
                          proposer_account_id,confirmer_account_id,
                          child_batch_id,effect_generation,effect_error,
                          source_app_id,source_channel,source_category,
                          source_dept
                        """
                    ),
                    {
                        "id": resolution_id,
                        "account_id": principal.account_id,
                        "app_id": current["app_id"],
                        "channel": current["channel"],
                        "category": current["category"],
                        "dept": source_dept,
                    },
                )
            ).mappings().one_or_none()
            if updated is None:
                raise UncertainResolutionConflict("处置单已确认")
            generation = int(updated["effect_generation"])
            await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="uncertain.effect",
                    aggregate_type="sms_uncertain_resolution",
                    aggregate_id=str(resolution_id),
                    task_name="app.tasks.outbox.apply_uncertain_effect",
                    queue="realtime",
                    args=(resolution_id,),
                    dedup_key=f"uncertain.effect:{resolution_id}:{generation}",
                ),
            )
            resolution = _row(updated)
            await insert_audit(
                connection,
                AuditEvent(
                    principal=principal,
                    role=principal.role,
                    ip=ip,
                    action="uncertain_resolve_confirm",
                    object_type="sms_uncertain_resolution",
                    object_id=str(resolution.id),
                    before={
                        "resolution_id": int(current["id"]),
                        "chunk_id": int(current["chunk_id"]),
                        "batch_id": int(current["batch_id"]),
                        "action": str(current["action"]),
                        "status": str(current["state"]),
                        "proposer_account_id": int(current["proposer_account_id"]),
                        "generation": int(current["effect_generation"]),
                    },
                    after={
                        "resolution_id": resolution.id,
                        "chunk_id": resolution.chunk_id,
                        "batch_id": resolution.batch_id,
                        "action": resolution.action,
                        "status": resolution.state,
                        "proposer_account_id": resolution.proposer_account_id,
                        "confirmer_account_id": resolution.confirmer_account_id,
                        "generation": resolution.effect_generation,
                    },
                ),
            )
            return resolution
    finally:
        await engine.dispose()
