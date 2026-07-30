"""为 raw 解析与人工重放增加可恢复的互斥处理租约。"""

from __future__ import annotations

from alembic import op

revision = "0038_raw_replay_processing_lease"
down_revision = "0037_metrics_access_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增空闲租约列；历史未处理记录保持可立即领取。"""

    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_processing_lease
        ON raw_vendor_log(processing_started_at,id)
        WHERE processed=false
        """
    )
    # 0037 收窄 metrics 权限时必须保留队列深度聚合实际使用的两列。
    op.execute(
        """
        GRANT SELECT (queue,state) ON outbox_event TO sms_metrics
        """
    )


def downgrade() -> None:
    """仅在没有正在处理的 raw 记录时移除租约边界。"""

    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM raw_vendor_log
            WHERE processed=false
              AND processing_started_at IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade raw replay processing lease with active claims';
          END IF;
        END
        $downgrade$
        """
    )
    op.execute(
        """
        REVOKE SELECT (queue,state) ON outbox_event FROM sms_metrics
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_raw_processing_lease")
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          DROP COLUMN IF EXISTS processing_started_at
        """
    )
