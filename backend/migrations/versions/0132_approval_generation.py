"""审批决定绑定不可复用的审阅版本。"""

from __future__ import annotations

from alembic import op

revision = "0132_approval_generation"
down_revision = "0131_uncertain_approval_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史审批回填第 1 版；schema.sql 基线已含该列，两条路径幂等收敛。"""

    op.execute(
        """
        ALTER TABLE approval
          ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'approval'::regclass
              AND conname = 'ck_approval_generation_positive'
          ) THEN
            ALTER TABLE approval
              ADD CONSTRAINT ck_approval_generation_positive
              CHECK (generation >= 1);
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    """只供本地回滚演练；生产不执行降级。"""

    op.execute("ALTER TABLE approval DROP CONSTRAINT IF EXISTS ck_approval_generation_positive")
    op.execute("ALTER TABLE approval DROP COLUMN IF EXISTS generation")
