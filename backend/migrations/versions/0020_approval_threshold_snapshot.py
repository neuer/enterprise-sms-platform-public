"""审批单固化创建时的触发阈值。"""

from __future__ import annotations

from alembic import op

revision = "0020_approval_threshold"
down_revision = "0019_vendor_hmac_alias"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新审批保存精确快照；无法重建的历史审批显式标记为未知。"""

    op.execute(
        "ALTER TABLE approval ADD COLUMN IF NOT EXISTS trigger_threshold INTEGER"
    )
    op.execute(
        "ALTER TABLE approval ADD COLUMN IF NOT EXISTS "
        "trigger_threshold_source VARCHAR(16) NOT NULL DEFAULT 'snapshot'"
    )
    op.execute(
        "UPDATE approval SET trigger_threshold_source='legacy_unknown' "
        "WHERE trigger_threshold IS NULL"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='approval'::regclass
              AND conname='chk_approval_trigger_threshold'
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT chk_approval_trigger_threshold
              CHECK (
                (trigger_threshold_source='snapshot' AND trigger_threshold > 0)
                OR
                (trigger_threshold_source='legacy_unknown' AND trigger_threshold IS NULL)
              );
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid='approval'::regclass
              AND conname='chk_approval_trigger_threshold_source'
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT chk_approval_trigger_threshold_source
              CHECK (trigger_threshold_source IN ('snapshot','legacy_unknown'));
          END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    """移除审批阈值快照。"""

    op.drop_constraint(
        "chk_approval_trigger_threshold_source",
        "approval",
        type_="check",
    )
    op.drop_constraint("chk_approval_trigger_threshold", "approval", type_="check")
    op.drop_column("approval", "trigger_threshold_source")
    op.drop_column("approval", "trigger_threshold")
