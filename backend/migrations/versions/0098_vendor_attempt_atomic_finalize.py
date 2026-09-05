"""供应商 attempt 增加 inconsistent，禁止把已提交分片降为 uncertain。"""

from __future__ import annotations

from alembic import op

revision = "0098_vendor_attempt_atomic_finalize"
down_revision = "0097_auth_session_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS sms_vendor_attempt_outcome_check
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT sms_vendor_attempt_outcome_check
          CHECK (outcome IN (
            'not_invoked','rejected','submitted','uncertain','failed',
            'retry_scheduled','delayed','paused','stale',
            'invoking','cancelled_before_invoke','inconsistent'
          ))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sms_vendor_attempt
        SET outcome='uncertain'
        WHERE outcome='inconsistent'
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          DROP CONSTRAINT IF EXISTS sms_vendor_attempt_outcome_check
        """
    )
    op.execute(
        """
        ALTER TABLE sms_vendor_attempt
          ADD CONSTRAINT sms_vendor_attempt_outcome_check
          CHECK (outcome IN (
            'not_invoked','rejected','submitted','uncertain','failed',
            'retry_scheduled','delayed','paused','stale',
            'invoking','cancelled_before_invoke'
          ))
        """
    )
