"""回执超时调度事实跨任务共享；沿用 sms_send 的批次写权限。"""

from __future__ import annotations

from alembic import op

revision = "0109_report_timeout_fairness"
down_revision = "0108_chunk_failover_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sms_batch
          ADD COLUMN IF NOT EXISTS report_timeout_last_attempt_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS report_timeout_next_attempt_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS report_timeout_generation BIGINT NOT NULL DEFAULT 0
            CONSTRAINT ck_batch_timeout_generation CHECK (report_timeout_generation>=0),
          ADD COLUMN IF NOT EXISTS report_timeout_failures INTEGER NOT NULL DEFAULT 0
            CONSTRAINT ck_batch_timeout_failures CHECK (report_timeout_failures BETWEEN 0 AND 16)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_batch_timeout_fairness
        ON sms_batch(report_timeout_last_attempt_at NULLS FIRST,id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX idx_batch_timeout_fairness")
    op.execute("""
        ALTER TABLE sms_batch DROP COLUMN report_timeout_last_attempt_at,
          DROP COLUMN report_timeout_next_attempt_at,
          DROP COLUMN report_timeout_generation, DROP COLUMN report_timeout_failures
    """)
