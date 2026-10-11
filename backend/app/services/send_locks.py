"""发送事务共用 chunk → batch → attempt 锁序与持锁事实快照。"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class LockedSendContext:
    """只能在调用方事务存活期间使用的已锁定状态。"""

    chunk: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]


async def lock_send_context(
    connection: AsyncConnection,
    chunk_id: int,
) -> LockedSendContext | None:
    """按统一次序加锁，batch 字段只从取得锁后的查询结果读取。"""
    chunk = (
        (
            await connection.execute(
                text(
                    "SELECT c.id,c.status,c.batch_id,c.route_generation, "
                    "c.retry_count,c.vendor_msg, "
                    "c.next_vendor,c.route_policy_version,c.failover_from_attempt_id, "
                    "c.retry_not_before,c.vendor_attempt_count "
                    "FROM sms_chunk c WHERE c.id=:id FOR UPDATE OF c"
                ),
                {"id": chunk_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if chunk is None:
        return None
    batch = (
        (
            await connection.execute(
                text(
                    "SELECT id,status,category,route_policy_version FROM sms_batch "
                    "WHERE id=:id FOR UPDATE"
                ),
                {"id": int(chunk["batch_id"])},
            )
        )
        .mappings()
        .one_or_none()
    )
    if batch is None:
        return None
    attempts = (
        (
            await connection.execute(
                text(
                    "SELECT id,chunk_id,vendor_id,generation,outcome,safe_to_failover,vendor_code "
                    "FROM sms_vendor_attempt WHERE chunk_id=:chunk_id "
                    "ORDER BY generation,id FOR UPDATE"
                ),
                {"chunk_id": chunk_id},
            )
        )
        .mappings()
        .all()
    )
    state = dict(chunk)
    state.update(
        category=batch["category"],
        batch_status=batch["status"],
        batch_policy_version=batch["route_policy_version"],
    )
    return LockedSendContext(state, tuple(dict(row) for row in attempts))


async def lock_send_chunks(
    connection: AsyncConnection,
    chunk_ids: list[int],
) -> None:
    """批量事务按 chunk、batch、attempt、app balance 顺序预锁完整集合。"""
    if chunk_ids:
        await connection.execute(
            text("SELECT id FROM sms_chunk WHERE id=ANY(:ids) ORDER BY id FOR UPDATE"),
            {"ids": sorted(set(chunk_ids))},
        )
        await connection.execute(
            text(
                "SELECT id FROM sms_batch WHERE id IN "
                "(SELECT batch_id FROM sms_chunk WHERE id=ANY(:ids)) "
                "ORDER BY id FOR UPDATE"
            ),
            {"ids": sorted(set(chunk_ids))},
        )
        await connection.execute(
            text(
                "SELECT id FROM sms_vendor_attempt WHERE chunk_id=ANY(:ids) "
                "ORDER BY chunk_id,generation,id FOR UPDATE"
            ),
            {"ids": sorted(set(chunk_ids))},
        )
        # 不按 chunk 遍历顺序获取多个 app 的余额。缺失余额仍由业务原语失败关闭，
        # 此处只锁已有预留对应的事实行，不为历史无预留批次创造账本。
        await connection.execute(
            text(
                "SELECT app_id FROM send_inflight_balance WHERE app_id IN ("
                "SELECT r.app_id FROM send_inflight_reservation r "
                "JOIN sms_batch b ON r.batch_id=b.id OR r.id=b.send_inflight_reservation_id "
                "JOIN sms_chunk c ON c.batch_id=b.id WHERE c.id=ANY(:ids)) "
                "ORDER BY app_id FOR UPDATE"
            ),
            {"ids": sorted(set(chunk_ids))},
        )
