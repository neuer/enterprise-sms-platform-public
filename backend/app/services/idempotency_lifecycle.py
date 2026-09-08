"""幂等结果共享生命周期；批次锁先于 Claim 锁，数据库时间决定是否可退役。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.idempotency import IdempotencyConflict

# 仅可解释的已完成事实允许退役；未知状态及缺失关联事实保守保留。
RESULT_WORK_PROTECTED_SQL = """
(
  b.id IS NULL
  OR b.status NOT IN ('completed','rejected','expired','cancelled')
  OR EXISTS (
    SELECT 1 FROM sms_chunk c WHERE c.batch_id=b.id
      AND c.status NOT IN ('submitted','failed')
  )
  OR EXISTS (
    SELECT 1 FROM callback_task t WHERE t.batch_id=b.id
      AND t.status NOT IN ('done','dead')
  )
  OR (b.status='completed' AND b.total>0
      AND NOT EXISTS (SELECT 1 FROM sms_chunk c WHERE c.batch_id=b.id))
)
"""

IDEMPOTENCY_LIVE_SQL = """(
 i.expires_at IS NULL OR i.expires_at > now()
 OR i.request_hash IS NULL OR i.request_hash_key_version IS NULL
 OR """ + RESULT_WORK_PROTECTED_SQL + ")"

CLAIM_RESULT_LIVE_SQL = """(
 COALESCE(i.expires_at,q.result_expires_at) IS NULL
 OR COALESCE(i.expires_at,q.result_expires_at) > now()
 OR """ + RESULT_WORK_PROTECTED_SQL + ")"

CLAIM_RESULT_SQL = """
 SELECT q.token,q.fingerprint,q.generation,q.state,q.batch_id,
   q.expires_at > now() AS lease_valid,
   i.id AS result_id,
   (CASE WHEN i.id IS NOT NULL THEN
      trim(i.request_hash)=trim(q.fingerprint) AND i.request_hash_key_version IS NOT NULL
    ELSE q.result_expires_at IS NOT NULL END) AS result_verified,
 """ + CLAIM_RESULT_LIVE_SQL + """ AS result_protected
 FROM idempotency_claim q
 LEFT JOIN sms_batch b ON b.id=q.batch_id
 LEFT JOIN idempotency_record i ON i.batch_id=q.batch_id
   AND i.scope_kind=q.scope_kind AND i.scope_id=q.scope_id AND i.biz_id=q.biz_id
 WHERE q.scope_kind=:scope_kind AND q.scope_id=:scope_id AND q.biz_id=:biz_id
"""


def require_completed_result(row: dict[str, Any]) -> None:
    """已完成操作缺少可信原结果时拒绝重发，不把它当作新业务键。"""
    if row["state"] == "completed" and (
        not row["result_verified"]
        or (row["result_id"] is None and row["result_protected"])
    ):
        raise IdempotencyConflict("原幂等结果无法验证，请核对原批次；禁止自动重发")


async def reclaim_completed_result(
    connection: AsyncConnection,
    params: dict[str, Any],
    batch_id: int | None,
) -> int | None:
    """持有批次与原 Claim 后重验事实，原子退役结果并推进 generation。"""
    if batch_id is None:
        raise IdempotencyConflict("原幂等结果无法验证，请核对原批次；禁止自动重发")
    locked = await connection.scalar(
        text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"), {"id": batch_id},
    )
    if locked is None:
        raise IdempotencyConflict("原幂等结果无法验证，请核对原批次；禁止自动重发")
    # 单独获取 Claim 锁，再用新语句快照读相关结果与工作，避免等待期间的旧快照。
    await connection.execute(text("""
        SELECT id FROM idempotency_claim
        WHERE scope_kind=:scope_kind AND scope_id=:scope_id AND biz_id=:biz_id FOR UPDATE
    """), params)
    row = (await connection.execute(text(CLAIM_RESULT_SQL), params)).mappings().one_or_none()
    if row is None or row["state"] != "completed" or row["batch_id"] != batch_id:
        return None
    require_completed_result(dict(row))
    if row["result_protected"]:
        return None
    if row["result_id"] is not None:
        removed = await connection.scalar(text("""
            DELETE FROM idempotency_record i USING sms_batch b
            WHERE i.id=:result_id AND i.batch_id=b.id AND NOT
        """ + IDEMPOTENCY_LIVE_SQL + " RETURNING i.id"), {"result_id": row["result_id"]})
        if removed is None:
            return None
    value = await connection.scalar(text("""
        UPDATE idempotency_claim SET
          generation=generation+1,token=:token,fingerprint=:fingerprint,
          expires_at=now()+make_interval(secs=>:ttl_s),state='active',
          batch_id=NULL,completed_at=NULL,released_at=NULL,release_reason=NULL,
          result_expires_at=NULL,updated_at=now()
        WHERE scope_kind=:scope_kind AND scope_id=:scope_id AND biz_id=:biz_id
          AND state='completed' AND batch_id=:batch_id AND generation=:generation
        RETURNING generation
    """), {**params, "batch_id": batch_id, "generation": row["generation"]})
    if value is None:
        raise IdempotencyConflict("幂等结果退役发生冲突，请核对原批次")
    return int(value)
