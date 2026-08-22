"""raw_vendor_log.capture_state：完整 / 超限完整 / 截断捕获。"""

from __future__ import annotations

from alembic import op

revision = "0072_raw_capture_state"
down_revision = "0071_reply_event_is_optout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线建库已含该列；存量升级在此补齐，两条路径幂等收敛。
    # 历史行都是完整落库路径，统一保持 complete。
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS capture_state VARCHAR(24) NOT NULL DEFAULT 'complete'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='ck_raw_vendor_capture_state'
              AND conrelid='raw_vendor_log'::regclass
          ) THEN
            ALTER TABLE raw_vendor_log
              ADD CONSTRAINT ck_raw_vendor_capture_state
              CHECK (capture_state IN ('complete','complete_too_large','truncated'));
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE raw_vendor_log DROP CONSTRAINT IF EXISTS ck_raw_vendor_capture_state"
    )
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS capture_state")
