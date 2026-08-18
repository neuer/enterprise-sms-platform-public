"""晚到回执/人工修复标记归属日脏集，统计聚合不再受固定 5 天窗口限制。"""

from __future__ import annotations

from alembic import op

revision = "0070_stat_dirty_date"
down_revision = "0069_raw_replay_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stat_dirty_date (
            stat_date  DATE PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("GRANT SELECT, INSERT ON stat_dirty_date TO sms_accept")
    op.execute("GRANT SELECT, INSERT, DELETE ON stat_dirty_date TO sms_send")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stat_dirty_date")
