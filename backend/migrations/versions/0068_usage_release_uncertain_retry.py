"""补齐 uncertain-retry 释放事件并允许释放排水期间重建预留。"""

from __future__ import annotations

from alembic import op

revision = "0068_usage_release_uncertain_retry"
down_revision = "0067_usage_projection_idempotency_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 与 0067 相同的幂等策略：schema.sql 基线建库与存量升级都会执行本迁移，
    # DROP+ADD 使两条建库路径收敛到同一最终定义。
    op.execute(
        "ALTER TABLE usage_reservation "
        "DROP CONSTRAINT IF EXISTS ck_usage_release_event_no_pii"
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD CONSTRAINT ck_usage_release_event_no_pii CHECK (
            release_event_id IS NULL
            OR release_event_id ~ (
              '^(batch[:][0-9a-f]{32}[:]cancelled|'
              ||'approval[:][1-9][0-9]*[:](rejected|expired)|usage[:]'
              ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              ||'[89ab][0-9a-f]{3}-[0-9a-f]{12}[:]'
              ||'(acceptance-failed|all-filtered|idempotent-reuse|'
              ||'orphan-recovery|uncertain-retry))$'
            )
          )
        """
    )
    # release_requested 行只在排水（等待 outbox apply_release）；把它排除出
    # 活跃唯一空间，同 request_key 才能在释放完成前重建预留。
    op.execute("DROP INDEX IF EXISTS uk_usage_reservation_active_request")
    op.execute(
        """
        CREATE UNIQUE INDEX uk_usage_reservation_active_request
          ON usage_reservation(request_key)
          WHERE state NOT IN ('released','release_requested')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uk_usage_reservation_active_request")
    op.execute(
        """
        CREATE UNIQUE INDEX uk_usage_reservation_active_request
          ON usage_reservation(request_key)
          WHERE state<>'released'
        """
    )
    op.execute(
        "ALTER TABLE usage_reservation "
        "DROP CONSTRAINT IF EXISTS ck_usage_release_event_no_pii"
    )
    op.execute(
        """
        ALTER TABLE usage_reservation
          ADD CONSTRAINT ck_usage_release_event_no_pii CHECK (
            release_event_id IS NULL
            OR release_event_id ~ (
              '^(batch[:][0-9a-f]{32}[:]cancelled|'
              ||'approval[:][1-9][0-9]*[:](rejected|expired)|usage[:]'
              ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
              ||'[89ab][0-9a-f]{3}-[0-9a-f]{12}[:]'
              ||'(acceptance-failed|all-filtered|idempotent-reuse|'
              ||'orphan-recovery))$'
            )
          )
        """
    )
