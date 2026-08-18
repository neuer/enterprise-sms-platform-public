"""raw 自动重放次数上限，毒丸退出自动窗口仅可人工重放。"""

from __future__ import annotations

from alembic import op

revision = "0069_raw_replay_attempts"
down_revision = "0068_usage_release_uncertain_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema.sql 基线建库已含该列；存量升级在此补齐，两条路径幂等收敛。
    op.execute(
        """
        ALTER TABLE raw_vendor_log
          ADD COLUMN IF NOT EXISTS replay_attempts INTEGER NOT NULL DEFAULT 0
          CHECK (replay_attempts>=0)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE raw_vendor_log DROP COLUMN IF EXISTS replay_attempts")
