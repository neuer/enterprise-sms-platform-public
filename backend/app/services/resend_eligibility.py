"""失败重发资格。普通首发必须等回执到齐；部分覆盖只能走显式补发。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.pipeline_contracts import AcceptCommitConflict, FailedSourceReference

TERMINAL_RESEND_STATUSES = frozenset({"completed", "completed_unknown"})

_REASON_TEXT = {
    "receipts_pending": "仍有回执未到齐，不能重发失败号码",
    "not_terminal": "批次尚未完成，不能重发失败号码",
    "supplement_required": "已有重发未覆盖全部失败号码，请补发未覆盖号码，不要更换 biz_id",
    "coverage_needs_review": "已有重发无法唯一对应原失败号码，需要人工核对",
    "nothing_uncovered": "没有未覆盖的失败号码",
    "preview_stale": "补发预览中的号码已不再是失败，请重新预览",
}


class ResendBlocked(AcceptCommitConflict):
    """普通重发不能开始。reason 是稳定枚举。"""

    def __init__(
        self,
        reason: str,
        *,
        covered: int = 0,
        uncovered: int = 0,
        child_batch_no: str | None = None,
    ) -> None:
        super().__init__(_REASON_TEXT.get(reason, "当前不能重发失败号码"))
        self.reason = reason
        self.covered = covered
        self.uncovered = uncovered
        self.child_batch_no = child_batch_no


@dataclass(frozen=True, slots=True)
class ResendCoverage:
    can_resend_failed: bool
    block_reason: str | None
    child_batch_no: str | None
    failed_count: int
    covered_failed: int
    uncovered_failed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "can_resend_failed": self.can_resend_failed,
            "resend_block_reason": self.block_reason,
            "resend_child_batch_no": self.child_batch_no,
            "resend_covered_failed": self.covered_failed,
            "resend_uncovered_failed": self.uncovered_failed,
        }


def decide_resend(
    *,
    status: str,
    active: int,
    failed: int,
    covered: int,
    child_batch_no: str | None,
    needs_review: bool,
) -> ResendCoverage:
    """根据锁定后的计数决定普通重发是否可以开始。"""

    uncovered = max(failed - covered, 0)
    if active > 0:
        reason = "receipts_pending"
    elif status not in TERMINAL_RESEND_STATUSES:
        reason = "not_terminal"
    elif needs_review:
        reason = "coverage_needs_review"
    elif child_batch_no is not None and uncovered > 0:
        reason = "supplement_required"
    else:
        reason = None
    allowed = reason is None and failed > 0
    return ResendCoverage(
        allowed,
        reason,
        child_batch_no,
        failed,
        covered,
        uncovered,
    )


COVERAGE_SQL = """
SELECT b.status,
  trim(child.batch_no) AS child_batch_no,
  COALESCE(action.coverage_needs_review, false) AS needs_review,
  count(*) FILTER (WHERE m.status IN ('pending','sent')) AS active_count,
  count(*) FILTER (WHERE m.status='failed') AS failed_count,
  count(*) FILTER (
    WHERE m.status='failed' AND EXISTS (
      SELECT 1 FROM sms_resend_supplement_member mem
      WHERE mem.source_batch_id=b.id
        AND mem.source_message_id=m.id
        AND mem.source_message_created_at=m.created_at
        AND mem.claim_state IN ('frozen','submitted')
    )
  ) AS covered_failed
FROM sms_batch b
LEFT JOIN sms_message m ON m.batch_id=b.id
LEFT JOIN sms_resend_action action ON action.source_batch_id=b.id
LEFT JOIN sms_batch child ON child.id=action.child_batch_id
WHERE b.batch_no=CAST(:batch_no AS char(32)) AND {predicate}
GROUP BY b.id, b.status, child.batch_no, action.coverage_needs_review
"""


async def read_coverage(
    connection: AsyncConnection,
    batch_no: str,
    predicate: str,
    params: dict[str, object],
) -> ResendCoverage | None:
    """读取当前权限内的重发资格。批次不存在时返回 None。"""

    result = await connection.execute(
        text(COVERAGE_SQL.format(predicate=predicate)),
        {"batch_no": batch_no, **params},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    child = row["child_batch_no"]
    return decide_resend(
        status=str(row["status"]),
        active=int(row["active_count"]),
        failed=int(row["failed_count"]),
        covered=int(row["covered_failed"]),
        child_batch_no=None if child is None else str(child),
        needs_review=bool(row["needs_review"]),
    )


async def reject_open_receipts(connection: AsyncConnection, batch_id: int, status: str) -> None:
    """源批次行锁内拒绝未完成回执。调用方不得在此之前插入子批次。"""

    if status not in TERMINAL_RESEND_STATUSES:
        raise ResendBlocked("not_terminal")
    result = await connection.execute(
        text(
            """
            SELECT count(*) FROM sms_message
            WHERE batch_id=:batch_id AND status IN ('pending','sent')
            """
        ),
        {"batch_id": batch_id},
    )
    if int(result.scalar_one()) > 0:
        raise ResendBlocked("receipts_pending")


async def record_ordinary_members(
    connection: AsyncConnection,
    *,
    child_batch_id: int,
    sources: tuple[FailedSourceReference, ...],
) -> None:
    """把本次普通重发覆盖的原消息记为已提交，供后续补发排除。"""

    if not sources:
        return
    await connection.execute(
        text(
            """
            INSERT INTO sms_resend_supplement_member(
              source_batch_id, source_message_id, source_message_created_at,
              child_batch_id, generation, claim_state
            )
            SELECT b.resend_of, src.id, src.created_at, b.id, 1, 'submitted'
            FROM sms_batch b
            JOIN unnest(
              CAST(:ids AS bigint[]), CAST(:created AS timestamptz[])
            ) AS src(id, created_at) ON true
            WHERE b.id=:child_id AND b.resend_of IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "child_id": child_batch_id,
            "ids": [item.message_id for item in sources],
            "created": [item.created_at for item in sources],
        },
    )
