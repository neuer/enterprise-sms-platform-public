"""为真实 UAT pre-batch 阶段增加可恢复的数据库租约。"""

from __future__ import annotations

from alembic import op

revision = "0023_vendor_uat_acceptance_lease"
down_revision = "0022_vendor_test_reset_operation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留既有 operation，并把无 batch 的遗留 UAT 置为可对账过期租约。"""

    op.execute(
        """
        ALTER TABLE vendor_test_operation
          ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE vendor_test_operation
        SET lease_expires_at=requested_at+interval '60 seconds'
        WHERE operation_type='uat_send'
          AND status IN ('requested','running')
          AND batch_no IS NULL
          AND lease_expires_at IS NULL
        """
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='chk_vendor_test_operation_uat_lease'
              AND conrelid='vendor_test_operation'::regclass
          ) THEN
            ALTER TABLE vendor_test_operation
              ADD CONSTRAINT chk_vendor_test_operation_uat_lease CHECK (
                (operation_type<>'uat_send' AND lease_expires_at IS NULL)
                OR (
                  operation_type='uat_send'
                  AND (
                    (
                      status IN ('requested','running')
                      AND (
                        (batch_no IS NULL AND lease_expires_at IS NOT NULL)
                        OR (batch_no IS NOT NULL AND lease_expires_at IS NULL)
                      )
                    )
                    OR (
                      status IN ('succeeded','failed')
                      AND lease_expires_at IS NULL
                    )
                  )
                )
              )
              NOT VALID;
            ALTER TABLE vendor_test_operation
              VALIDATE CONSTRAINT chk_vendor_test_operation_uat_lease;
          END IF;
        END
        $migration$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vendor_test_operation_uat_lease
        ON vendor_test_operation(lease_expires_at,id)
        WHERE operation_type='uat_send'
          AND status IN ('requested','running')
          AND batch_no IS NULL
        """
    )


def downgrade() -> None:
    """operation 崩溃恢复证据不可由自动迁移删除。"""

    raise RuntimeError("vendor UAT acceptance lease downgrade is intentionally blocked")
