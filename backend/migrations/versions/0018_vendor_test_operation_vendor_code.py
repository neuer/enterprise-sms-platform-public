"""为真实联调 operation 增加安全的整数厂商错误码。"""

from __future__ import annotations

from alembic import op

revision = "0018_vendor_code"
down_revision = "0017_vendor_test_web_console"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vendor_test_operation
          ADD COLUMN IF NOT EXISTS vendor_code INTEGER
          CHECK (vendor_code BETWEEN 1 AND 99999)
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='chk_vendor_test_operation_vendor_code'
              AND conrelid='vendor_test_operation'::regclass
          ) THEN
            ALTER TABLE vendor_test_operation
              ADD CONSTRAINT chk_vendor_test_operation_vendor_code CHECK (
                vendor_code IS NULL
                OR (status = 'failed' AND safe_code = 'VENDOR_ERROR')
              );
          END IF;
        END
        $migration$
        """
    )


def downgrade() -> None:
    """联调故障证据不可由自动迁移删除。"""

    raise RuntimeError("vendor test operation evidence downgrade is intentionally blocked")
