"""补发意图与子批次同事务提交，普通重发的唯一动作保持独立。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.pipeline_contracts import AcceptCommitConflict, BatchCommand, StoredBatch


async def lock_supplement(
    connection: AsyncConnection,
    command: BatchCommand,
    source_batch_id: int,
) -> StoredBatch | None:
    """在原批次锁内复核不可变意图及全部冻结成员，重复提交返回本意图的子批次。"""
    intent_id = command.resend_supplement_id
    if (
        intent_id is None
        or command.scope_kind != "resend-extra"
        or command.scope_id != str(intent_id)
    ):
        raise AcceptCommitConflict("补发意图与幂等作用域不匹配")
    result = await connection.execute(
        text("""
        SELECT s.state,s.child_batch_id,trim(b.batch_no) batch_no
        FROM sms_resend_supplement s
        LEFT JOIN sms_batch b ON b.id=s.child_batch_id
        WHERE s.id=:id AND s.source_batch_id=:source_id
        FOR UPDATE OF s
    """),
        {"id": intent_id, "source_id": source_batch_id},
    )
    intent = result.mappings().one_or_none()
    if intent is None or intent["state"] not in ("preview", "submitted"):
        raise AcceptCommitConflict("补发预览已失效，请重新预览")
    if intent["state"] == "submitted":
        return StoredBatch(str(intent["batch_no"]), True)
    members = await connection.execute(
        text("""
        SELECT source_message_id,source_message_created_at
        FROM sms_resend_supplement_member
        WHERE supplement_id=:id AND source_batch_id=:source_id AND claim_state='frozen'
        FOR UPDATE
    """),
        {"id": intent_id, "source_id": source_batch_id},
    )
    actual = {
        (int(row["source_message_id"]), row["source_message_created_at"])
        for row in members.mappings()
    }
    expected = {(item.message_id, item.created_at) for item in command.failed_sources}
    if not actual or actual != expected:
        raise AcceptCommitConflict("补发预览的号码集合已变化，请重新预览")
    return None


async def commit_supplement(
    connection: AsyncConnection,
    command: BatchCommand,
    child_batch_id: int,
) -> None:
    """子批次、意图、消息覆盖和发送 Outbox 共享调用方事务；失败全部回滚。"""
    updated = await connection.execute(
        text("""
        UPDATE sms_resend_supplement SET state='submitted',child_batch_id=:child,
          submitted_at=clock_timestamp()
        WHERE id=:id AND state='preview' RETURNING id
    """),
        {"id": command.resend_supplement_id, "child": child_batch_id},
    )
    if updated.scalar_one_or_none() is None:
        raise AcceptCommitConflict("补发意图已变化，请查询原补发结果")
    await connection.execute(
        text("""
        UPDATE sms_resend_supplement_member SET claim_state='submitted',child_batch_id=:child
        WHERE supplement_id=:id AND claim_state='frozen'
    """),
        {"id": command.resend_supplement_id, "child": child_batch_id},
    )


async def commit_resend(
    connection: AsyncConnection, command: BatchCommand, child_batch_id: int
) -> None:
    """在同一批次事务登记普通重发动作或独立补发意图及来源覆盖。"""
    if command.resend_supplement_id is not None:
        await commit_supplement(connection, command, child_batch_id)
        return
    action = await connection.execute(
        text("""
        INSERT INTO sms_resend_action(source_batch_id,child_batch_id)
        SELECT resend_of,id FROM sms_batch
        WHERE id=:batch_id AND resend_of IS NOT NULL
        RETURNING source_batch_id
    """),
        {"batch_id": child_batch_id},
    )
    if action.scalar_one_or_none() is None:
        raise ValueError("失败重发源批次不存在")
    from app.services.resend_eligibility import record_ordinary_members

    await record_ordinary_members(
        connection, child_batch_id=child_batch_id, sources=command.failed_sources
    )
